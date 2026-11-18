"""Stock API client with timeouts, bounded retries and explicit outcomes.

The client performs I/O only. Staleness is applied by ``check_stock`` using
the policy clock, which keeps the retry logic and the policy rules separable
and testable without a live server.
"""
import json
import random
import time
from dataclasses import dataclass, replace
from datetime import datetime
from pathlib import Path
from typing import (
    Callable,
    Dict,
    Iterable,
    List,
    Mapping,
    Optional,
    Protocol,
    Sequence,
)
from urllib.parse import quote

import httpx
from pydantic import ValidationError

from app.config import ApprovalRules, RetryPolicy
from app.models import (
    PricedQuote,
    SpecialistFlag,
    StockSnapshot,
    StockStatus,
)

RETRYABLE_STATUS = frozenset({429, 500, 502, 503, 504})
MAX_RETRY_AFTER_SECONDS = 30.0


class StockAPIError(Exception):
    """Base error for stock API calls."""


class StockUnavailableError(StockAPIError):
    """Retries were exhausted or a transport error kept recurring."""


class StockRequestError(StockAPIError):
    """A non-retryable response (for example HTTP 400)."""


@dataclass(frozen=True)
class StockReading:
    part_number: str
    status: StockStatus
    available: Optional[int] = None
    as_of: Optional[datetime] = None
    stale: bool = False
    age_hours: Optional[float] = None
    reason: Optional[str] = None


def reading_from_snapshot(snapshot: StockSnapshot) -> StockReading:
    on_hand = snapshot.total_quantity
    return StockReading(
        part_number=snapshot.part_number,
        status=StockStatus.OUT_OF_STOCK if on_hand == 0 else StockStatus.OK,
        available=on_hand,
        as_of=snapshot.as_of,
    )


def _chunks(values: Sequence[str], size: int) -> Iterable[List[str]]:
    for start in range(0, len(values), size):
        yield list(values[start : start + size])


class StockClient(Protocol):
    def fetch_many(self, part_numbers: Sequence[str]) -> Dict[str, StockReading]:
        ...


class HttpStockClient:
    """Synchronous stock API client.

    ``fetch_many`` never raises for an upstream outage: each affected part is
    returned as ``StockStatus.UNAVAILABLE`` so the quote can be flagged rather
    than blocked or guessed.
    """

    def __init__(
        self,
        base_url: str,
        retry: RetryPolicy,
        *,
        client: Optional[httpx.Client] = None,
        sleep: Callable[[float], None] = time.sleep,
    ) -> None:
        self._base_url = base_url.rstrip("/")
        self._retry = retry
        self._sleep = sleep
        self._client = client or httpx.Client(timeout=retry.timeout_seconds)
        self._owns_client = client is None

    def __enter__(self) -> "HttpStockClient":
        return self

    def __exit__(self, *exc_info) -> None:
        self.close()

    def close(self) -> None:
        if self._owns_client:
            self._client.close()

    # -- public API ---------------------------------------------------------

    def fetch_many(self, part_numbers: Sequence[str]) -> Dict[str, StockReading]:
        ordered = list(dict.fromkeys(p for p in part_numbers if p))
        if not ordered:
            return {}

        readings: Dict[str, StockReading] = {}
        for chunk in _chunks(ordered, self._retry.batch_size):
            readings.update(self._fetch_chunk(chunk))
        return {part: readings[part] for part in ordered}

    def fetch_one(self, part_number: str) -> StockReading:
        path = f"/v1/stock/{quote(part_number, safe='')}"
        try:
            payload = self._request_with_retries(path, None, allow_not_found=True)
        except StockAPIError as exc:
            return StockReading(
                part_number=part_number,
                status=StockStatus.UNAVAILABLE,
                reason=str(exc),
            )
        if payload is None:
            return StockReading(part_number=part_number, status=StockStatus.NOT_FOUND)
        return self._reading_from_payload(payload, part_number)

    # -- internals ----------------------------------------------------------

    def _fetch_chunk(self, parts: List[str]) -> Dict[str, StockReading]:
        try:
            payload = self._request_with_retries(
                "/v1/stock", {"part_numbers": ",".join(parts)}
            )
        except StockAPIError as exc:
            return {
                part: StockReading(
                    part_number=part,
                    status=StockStatus.UNAVAILABLE,
                    reason=str(exc),
                )
                for part in parts
            }

        if not isinstance(payload, dict):
            return {
                part: StockReading(
                    part_number=part,
                    status=StockStatus.UNAVAILABLE,
                    reason="stock API returned an unexpected payload",
                )
                for part in parts
            }

        readings: Dict[str, StockReading] = {}
        raw_items = payload.get("items", [])
        if isinstance(raw_items, list):
            for item in raw_items:
                part = item.get("part_number") if isinstance(item, dict) else None
                if not isinstance(part, str) or not part:
                    continue
                readings[part] = self._reading_from_payload(item, part)

        raw_not_found = payload.get("not_found", [])
        if isinstance(raw_not_found, list):
            for part in raw_not_found:
                if isinstance(part, str):
                    readings[part] = StockReading(
                        part_number=part, status=StockStatus.NOT_FOUND
                    )

        for part in parts:
            readings.setdefault(
                part,
                StockReading(
                    part_number=part,
                    status=StockStatus.UNAVAILABLE,
                    reason="part missing from stock API response",
                ),
            )
        return readings

    def _reading_from_payload(self, payload: Mapping, fallback_part: str) -> StockReading:
        try:
            snapshot = StockSnapshot.model_validate(payload)
        except ValidationError:
            return StockReading(
                part_number=fallback_part,
                status=StockStatus.UNAVAILABLE,
                reason="invalid stock API response",
            )
        return reading_from_snapshot(snapshot)

    def _request_with_retries(
        self,
        path: str,
        params: Optional[Mapping[str, str]],
        *,
        allow_not_found: bool = False,
    ) -> Optional[Mapping]:
        url = f"{self._base_url}{path}"
        last_error = "no attempt made"

        for attempt in range(1, self._retry.max_attempts + 1):
            retry_after: Optional[float] = None
            try:
                response = self._client.get(
                    url, params=params, timeout=self._retry.timeout_seconds
                )
            except (httpx.TimeoutException, httpx.TransportError) as exc:
                last_error = type(exc).__name__
            else:
                if response.status_code == 200:
                    try:
                        return response.json()
                    except ValueError:
                        last_error = "invalid JSON response"
                elif response.status_code == 404 and allow_not_found:
                    return None
                elif response.status_code in RETRYABLE_STATUS:
                    last_error = f"HTTP {response.status_code}"
                    retry_after = self._retry_after_seconds(response)
                else:
                    raise StockRequestError(
                        f"stock API returned HTTP {response.status_code}"
                    )

            if attempt < self._retry.max_attempts:
                self._sleep(self._delay_seconds(attempt, retry_after))

        raise StockUnavailableError(
            f"stock API unavailable after {self._retry.max_attempts} attempts "
            f"({last_error})"
        )

    def _retry_after_seconds(self, response: httpx.Response) -> Optional[float]:
        value: Optional[float] = None
        try:
            payload = response.json()
        except ValueError:
            payload = {}
        if isinstance(payload, dict):
            candidate = payload.get("retry_after_seconds")
            if isinstance(candidate, (int, float)):
                value = float(candidate)
        if value is None:
            header = response.headers.get("Retry-After")
            if header:
                try:
                    value = float(header)
                except ValueError:
                    value = None
        if value is None:
            return None
        return min(max(value, 0.0), MAX_RETRY_AFTER_SECONDS)

    def _delay_seconds(self, attempt: int, retry_after: Optional[float]) -> float:
        delay = min(
            self._retry.max_backoff_seconds,
            self._retry.initial_backoff_seconds * (2 ** (attempt - 1)),
        )
        if retry_after is not None:
            delay = max(delay, retry_after)
        if self._retry.jitter:
            delay *= random.uniform(0.5, 1.5)
        return min(delay, MAX_RETRY_AFTER_SECONDS)


class StaticStockClient:
    """Fixture-backed client for tests and offline demos."""

    def __init__(
        self,
        snapshots: Mapping[str, StockSnapshot],
        unavailable: Iterable[str] = (),
    ) -> None:
        self._snapshots = dict(snapshots)
        self._unavailable = set(unavailable)

    @classmethod
    def from_fixture(
        cls, path: Path, unavailable: Iterable[str] = ()
    ) -> "StaticStockClient":
        raw = json.loads(Path(path).read_text(encoding="utf-8"))
        snapshots = {
            part: StockSnapshot.model_validate(payload)
            for part, payload in raw.items()
        }
        return cls(snapshots, unavailable=unavailable)

    def fetch_many(self, part_numbers: Sequence[str]) -> Dict[str, StockReading]:
        ordered = list(dict.fromkeys(p for p in part_numbers if p))
        readings: Dict[str, StockReading] = {}
        for part in ordered:
            if part in self._unavailable:
                readings[part] = StockReading(
                    part_number=part,
                    status=StockStatus.UNAVAILABLE,
                    reason="simulated stock API failure",
                )
            elif part in self._snapshots:
                readings[part] = reading_from_snapshot(self._snapshots[part])
            else:
                readings[part] = StockReading(
                    part_number=part, status=StockStatus.NOT_FOUND
                )
        return readings

    def fetch_one(self, part_number: str) -> StockReading:
        return self.fetch_many([part_number])[part_number]


def check_stock(
    client: StockClient,
    part_numbers: Sequence[str],
    now: datetime,
    rules: ApprovalRules,
) -> Dict[str, StockReading]:
    """Fetch and mark readings older than the policy limit as stale."""
    readings = client.fetch_many(part_numbers)
    limit = rules.thresholds.stock_stale_hours
    checked: Dict[str, StockReading] = {}

    for part, reading in readings.items():
        if reading.as_of is None or reading.status is StockStatus.UNAVAILABLE:
            checked[part] = reading
            continue
        as_of = reading.as_of
        if as_of.tzinfo is None and now.tzinfo is not None:
            as_of = as_of.replace(tzinfo=now.tzinfo)
        age_hours = (now - as_of).total_seconds() / 3600.0
        checked[part] = replace(
            reading,
            stale=age_hours > limit,
            age_hours=age_hours,
            as_of=as_of,
        )
    return checked


def apply_stock(priced: PricedQuote, readings: Mapping[str, StockReading]) -> PricedQuote:
    """Copy stock details onto the priced lines for storage/display."""
    lines = []
    for line in priced.lines:
        reading = readings.get(line.part_number)
        if reading is None:
            lines.append(line)
            continue
        lines.append(
            line.model_copy(
                update={
                    "stock_status": reading.status,
                    "stock_as_of": reading.as_of,
                    "stock_available": reading.available,
                }
            )
        )
    return priced.model_copy(update={"lines": lines})


def stock_flags(
    priced: PricedQuote,
    readings: Mapping[str, StockReading],
    rules: ApprovalRules,
) -> List[SpecialistFlag]:
    """Translate stock outcomes into specialist-review flags.

    ``not_found``, ``out_of_stock`` and stale readings are line-level.
    An API outage after retries is quote-level because we cannot price the
    stock position for any line.
    """
    flags: List[SpecialistFlag] = []
    unavailable_parts: List[str] = []
    limit = rules.thresholds.stock_stale_hours

    for line in priced.lines:
        reading = readings.get(line.part_number)
        if reading is None:
            unavailable_parts.append(line.part_number)
            continue
        if reading.status is StockStatus.UNAVAILABLE:
            unavailable_parts.append(line.part_number)
            continue
        if reading.status is StockStatus.NOT_FOUND:
            flags.append(
                SpecialistFlag(
                    line=line.line,
                    reason=(
                        f"part {line.part_number} not found in stock API; "
                        "specialist decides backorder, substitute or remove"
                    ),
                )
            )
            continue
        if reading.status is StockStatus.OUT_OF_STOCK:
            flags.append(
                SpecialistFlag(
                    line=line.line,
                    reason=(
                        f"part {line.part_number} out of stock in all warehouses; "
                        "specialist decides backorder, substitute or remove"
                    ),
                )
            )
        if reading.stale:
            age = reading.age_hours if reading.age_hours is not None else 0.0
            flags.append(
                SpecialistFlag(
                    line=line.line,
                    reason=(
                        f"stock reading is {age:.1f}h old (limit {limit}h); "
                        "confirm before sending"
                    ),
                )
            )

    if unavailable_parts:
        parts = ", ".join(sorted(set(unavailable_parts)))
        flags.append(
            SpecialistFlag(
                line=None,
                reason=f"stock unavailable after retries ({parts})",
            )
        )
    return flags
