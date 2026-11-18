"""Stock client: batching, retries, timeouts and policy flags."""
from datetime import timedelta
from pathlib import Path

import httpx
import pytest

from app.config import RetryPolicy
from app.models import StockSnapshot, StockStatus
from app.pricing import price_quote
from app.stock import (
    HttpStockClient,
    StaticStockClient,
    apply_stock,
    check_stock,
    stock_flags,
)

ROOT = Path(__file__).resolve().parent.parent
FIXTURE = ROOT / "stock_fixture.json"


def _retry(**overrides):
    values = dict(
        max_attempts=3,
        timeout_seconds=1.0,
        initial_backoff_seconds=0.5,
        max_backoff_seconds=4.0,
        batch_size=50,
        jitter=False,
    )
    values.update(overrides)
    return RetryPolicy(**values)


def _client(handler, retry=None, sleeps=None):
    retry = retry or _retry()
    transport = httpx.MockTransport(handler)
    http = httpx.Client(transport=transport, base_url="http://stock.test")
    return HttpStockClient(
        "http://stock.test",
        retry,
        client=http,
        sleep=sleeps.append if sleeps is not None else (lambda _seconds: None),
    )


def _batch_handler(items_by_part):
    def handler(request):
        parts = request.url.params["part_numbers"].split(",")
        items = [items_by_part[p] for p in parts if p in items_by_part]
        not_found = [p for p in parts if p not in items_by_part]
        return httpx.Response(200, json={"items": items, "not_found": not_found})

    return handler


def test_fetch_many_maps_ok_out_of_stock_and_not_found():
    as_of = "2026-10-12T08:00:00-05:00"
    handler = _batch_handler(
        {
            "A": {"part_number": "A", "warehouses": {"DAL": 5, "PHX": 0}, "as_of": as_of},
            "B": {"part_number": "B", "warehouses": {"DAL": 0, "PHX": 0}, "as_of": as_of},
        }
    )
    client = _client(handler)

    readings = client.fetch_many(["A", "B", "C"])

    assert readings["A"].status is StockStatus.OK
    assert readings["A"].available == 5
    assert readings["B"].status is StockStatus.OUT_OF_STOCK
    assert readings["B"].available == 0
    assert readings["C"].status is StockStatus.NOT_FOUND


def test_batches_are_capped_at_50_parts():
    query_sizes = []

    def handler(request):
        parts = request.url.params["part_numbers"].split(",")
        query_sizes.append(len(parts))
        items = [
            {
                "part_number": part,
                "warehouses": {"DAL": 1},
                "as_of": "2026-10-12T08:00:00-05:00",
            }
            for part in parts
        ]
        return httpx.Response(200, json={"items": items, "not_found": []})

    client = _client(handler)
    readings = client.fetch_many([f"P{index}" for index in range(120)])

    assert query_sizes == [50, 50, 20]
    assert len(readings) == 120


def test_retries_with_backoff_then_succeeds():
    attempts = []
    sleeps = []

    def handler(request):
        attempts.append(1)
        if len(attempts) < 3:
            return httpx.Response(
                503,
                json={"error": "service_unavailable", "retry_after_seconds": 2},
            )
        return httpx.Response(
            200,
            json={
                "items": [
                    {
                        "part_number": "A",
                        "warehouses": {"DAL": 7},
                        "as_of": "2026-10-12T08:00:00-05:00",
                    }
                ],
                "not_found": [],
            },
        )

    client = _client(handler, sleeps=sleeps)
    readings = client.fetch_many(["A"])

    assert len(attempts) == 3
    assert readings["A"].status is StockStatus.OK
    # retry_after_seconds (2s) wins over the shorter exponential backoff.
    assert sleeps == [2.0, 2.0]


def test_exhausted_retries_mark_parts_unavailable():
    attempts = []
    sleeps = []

    def handler(request):
        attempts.append(1)
        return httpx.Response(
            503, json={"error": "service_unavailable", "retry_after_seconds": 2}
        )

    client = _client(handler, retry=_retry(max_attempts=3), sleeps=sleeps)
    readings = client.fetch_many(["A", "B"])

    assert len(attempts) == 3
    assert len(sleeps) == 2
    assert readings["A"].status is StockStatus.UNAVAILABLE
    assert readings["B"].status is StockStatus.UNAVAILABLE
    assert "after 3 attempts" in readings["A"].reason


def test_transport_errors_are_retried():
    attempts = []

    def handler(request):
        attempts.append(1)
        raise httpx.ConnectError("connection refused", request=request)

    client = _client(handler, retry=_retry(max_attempts=3))
    readings = client.fetch_many(["A"])

    assert len(attempts) == 3
    assert readings["A"].status is StockStatus.UNAVAILABLE
    assert "ConnectError" in readings["A"].reason


def test_non_retryable_http_error_is_not_retried():
    attempts = []

    def handler(request):
        attempts.append(1)
        return httpx.Response(400, json={"error": "bad_request"})

    client = _client(handler, retry=_retry(max_attempts=4))
    readings = client.fetch_many(["A"])

    assert len(attempts) == 1
    assert readings["A"].status is StockStatus.UNAVAILABLE
    assert "HTTP 400" in readings["A"].reason


def test_fetch_one_404_is_not_found():

    def handler(request):
        return httpx.Response(
            404, json={"error": "not_found", "part_number": "ZZZ"}
        )

    reading = _client(handler).fetch_one("ZZZ")

    assert reading.status is StockStatus.NOT_FOUND


def test_empty_part_list_makes_no_request():
    def handler(request):  # pragma: no cover - failure path
        raise AssertionError("no request expected")

    assert _client(handler).fetch_many([]) == {}


def test_check_stock_marks_only_readings_older_than_limit(settings):
    now = settings.now()
    client = StaticStockClient(
        {
            "FRESH": StockSnapshot(
                part_number="FRESH",
                warehouses={"DAL": 4},
                as_of=now - timedelta(hours=24),
            ),
            "OLD": StockSnapshot(
                part_number="OLD",
                warehouses={"DAL": 4},
                as_of=now - timedelta(hours=24, minutes=30),
            ),
        }
    )

    readings = check_stock(client, ["FRESH", "OLD"], now, settings.rules)

    assert not readings["FRESH"].stale
    assert readings["OLD"].stale
    assert readings["OLD"].age_hours == pytest.approx(24.5)


def test_out_of_stock_sample_is_flagged_on_the_line(
    drafts_by_id, reporting, settings
):
    priced = price_quote(
        drafts_by_id["DQ-24108"], reporting, settings.rules, settings.now()
    )
    static = StaticStockClient.from_fixture(FIXTURE)
    readings = check_stock(
        static, [line.part_number for line in priced.lines], settings.now(), settings.rules
    )

    flags = stock_flags(priced, readings, settings.rules)
    line_one = [flag for flag in flags if flag.line == 1]

    assert line_one
    assert "RL110322 out of stock in all warehouses" in line_one[0].reason
    assert not [flag for flag in flags if flag.line is None]


def test_missing_part_is_flagged_from_the_batch_response(
    drafts_by_id, reporting, settings
):
    priced = price_quote(
        drafts_by_id["DQ-24103"], reporting, settings.rules, settings.now()
    )
    static = StaticStockClient.from_fixture(FIXTURE)
    readings = check_stock(
        static, [line.part_number for line in priced.lines], settings.now(), settings.rules
    )

    flags = stock_flags(priced, readings, settings.rules)
    line_three = [flag for flag in flags if flag.line == 3]

    assert line_three
    assert "RL120374 not found in stock API" in line_three[0].reason


def test_stale_reading_is_flagged_with_age(drafts_by_id, reporting, settings):
    priced = price_quote(
        drafts_by_id["DQ-24104"], reporting, settings.rules, settings.now()
    )
    static = StaticStockClient.from_fixture(FIXTURE)
    readings = check_stock(
        static, [line.part_number for line in priced.lines], settings.now(), settings.rules
    )

    flags = stock_flags(priced, readings, settings.rules)
    line_one = [flag for flag in flags if flag.line == 1]

    assert line_one
    assert "49.5h old (limit 24h)" in line_one[0].reason


def test_unavailable_is_reported_once_for_the_whole_quote(
    drafts_by_id, reporting, settings
):
    priced = price_quote(
        drafts_by_id["DQ-24101"], reporting, settings.rules, settings.now()
    )
    static = StaticStockClient.from_fixture(
        FIXTURE, unavailable={"RL104165", "RL134671"}
    )
    readings = check_stock(
        static, [line.part_number for line in priced.lines], settings.now(), settings.rules
    )

    flags = stock_flags(priced, readings, settings.rules)
    quote_flags = [flag for flag in flags if flag.line is None]

    assert len(quote_flags) == 1
    assert "stock unavailable after retries" in quote_flags[0].reason
    assert "RL104165" in quote_flags[0].reason


def test_apply_stock_copies_status_onto_priced_lines(
    drafts_by_id, reporting, settings
):
    priced = price_quote(
        drafts_by_id["DQ-24108"], reporting, settings.rules, settings.now()
    )
    static = StaticStockClient.from_fixture(FIXTURE)
    readings = check_stock(
        static, [line.part_number for line in priced.lines], settings.now(), settings.rules
    )

    updated = apply_stock(priced, readings)

    assert updated.lines[0].stock_status is StockStatus.OUT_OF_STOCK
    assert updated.lines[0].stock_available == 0
    assert updated.lines[0].stock_as_of is not None
