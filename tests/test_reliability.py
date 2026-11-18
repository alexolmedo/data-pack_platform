"""Failure-mode coverage: stock outage and stale reporting data end to end."""
from datetime import timedelta
from pathlib import Path

import httpx

from app.config import RetryPolicy
from app.pipeline import process_drafts
from app.stock import HttpStockClient, StaticStockClient
from app.store import QuoteStore

ROOT = Path(__file__).resolve().parent.parent
FIXTURE = ROOT / "stock_fixture.json"


def _always_503_client() -> HttpStockClient:
    def handler(request):
        return httpx.Response(503, json={"error": "service_unavailable"})

    retry = RetryPolicy(
        max_attempts=3,
        timeout_seconds=1.0,
        initial_backoff_seconds=0.0,
        max_backoff_seconds=0.0,
        batch_size=50,
        jitter=False,
    )
    http = httpx.Client(transport=httpx.MockTransport(handler))
    return HttpStockClient(
        "http://stock.test",
        retry,
        client=http,
        sleep=lambda _seconds: None,
    )


def test_total_stock_outage_still_produces_quotes_and_visible_flags(
    tmp_path, drafts, reporting, settings
):
    store = QuoteStore(tmp_path / "reliability.db")
    output = tmp_path / "routing_results.json"

    results = process_drafts(
        drafts,
        reporting,
        _always_503_client(),
        store,
        settings.rules,
        settings.now(),
        output_path=output,
    )

    assert len(results) == 10
    assert output.is_file()
    for result in results:
        quote_flags = [
            flag
            for flag in result.specialist_review_flags
            if flag.line is None
        ]
        assert len(quote_flags) == 1
        assert "stock unavailable after retries" in quote_flags[0].reason

    # An outage is visible, not blocking: quotes and approval requests exist.
    assert store.get_quote("DQ-24101") is not None
    assert store.get_approval_request("DQ-24101") is not None
    assert len(store.list_audit()) == 10


def test_stale_reporting_export_is_flagged_end_to_end(
    tmp_path, drafts, reporting, settings
):
    store = QuoteStore(tmp_path / "stale.db")
    stale_now = settings.now() + timedelta(hours=24)  # export age 55.5h

    results = process_drafts(
        drafts,
        reporting,
        StaticStockClient.from_fixture(FIXTURE),
        store,
        settings.rules,
        stale_now,
        output_path=tmp_path / "routing_results.json",
    )

    by_id = {result.draft_id: result for result in results}
    quote_flags = [
        flag
        for flag in by_id["DQ-24101"].specialist_review_flags
        if flag.line is None
    ]

    assert any(
        "reporting export is 55.5h old" in flag.reason for flag in quote_flags
    )
    # Stock readings are now older than 24h too; they are line-level flags.
    assert any(
        flag.line == 1 and "h old (limit 24h)" in flag.reason
        for flag in by_id["DQ-24101"].specialist_review_flags
    )
