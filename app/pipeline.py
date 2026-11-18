"""End-to-end processing: load drafts, price, check stock, route and persist.

Run with ``python -m app.pipeline`` after starting ``mock_stock_api.py``.
Use ``--fixture`` for an offline run that reads ``stock_fixture.json``.
"""
import argparse
import json
import os
import tempfile
from datetime import datetime
from pathlib import Path
from typing import List, Optional, Sequence

from app.config import ApprovalRules, Settings, get_settings
from app.models import DraftQuote, RoutingResult
from app.pricing import PricingError, price_quote
from app.reporting import ReportingData, load_reporting_data
from app.routing import route_quote
from app.stock import (
    HttpStockClient,
    StaticStockClient,
    StockClient,
    apply_stock,
    check_stock,
    stock_flags,
)
from app.store import QuoteStore, StoreError


class PipelineError(Exception):
    """A draft could not be processed."""


def load_drafts(path: Path) -> List[DraftQuote]:
    try:
        payload = json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise PipelineError(f"cannot read drafts from {path}") from exc

    raw = payload.get("drafts") if isinstance(payload, dict) else payload
    if not isinstance(raw, list):
        raise PipelineError(f"{path}: expected a list under 'drafts'")

    drafts = [DraftQuote.model_validate(item) for item in raw]
    if not drafts:
        raise PipelineError(f"{path}: no drafts found")

    seen = set()
    for draft in drafts:
        if draft.draft_id in seen:
            raise PipelineError(f"{path}: duplicate draft_id {draft.draft_id}")
        seen.add(draft.draft_id)
    return drafts


def process_drafts(
    drafts: Sequence[DraftQuote],
    reporting: ReportingData,
    stock_client: StockClient,
    store: QuoteStore,
    rules: ApprovalRules,
    now: datetime,
    output_path: Optional[Path] = None,
) -> List[RoutingResult]:
    """Process drafts in input order and optionally write the results file."""
    results: List[RoutingResult] = []

    for draft in drafts:
        duplicate_of = store.find_duplicate(
            draft, now, rules.thresholds.duplicate_window_hours
        )
        try:
            priced = price_quote(draft, reporting, rules, now)
        except PricingError as exc:
            raise PipelineError(f"{draft.draft_id}: {exc}") from exc

        readings = check_stock(
            stock_client,
            [line.part_number for line in priced.lines],
            now,
            rules,
        )
        priced = apply_stock(priced, readings)
        decision = route_quote(
            draft,
            priced,
            reporting,
            rules,
            now,
            extra_flags=stock_flags(priced, readings, rules),
            duplicate_of=duplicate_of,
        )

        # Re-runs rebuild the output but never overwrite an existing quote or
        # its approval/audit history.
        if store.get_quote(draft.draft_id) is None:
            try:
                store.save_quote(draft, priced, decision, now)
            except StoreError as exc:
                raise PipelineError(
                    f"{draft.draft_id}: cannot persist quote: {exc}"
                ) from exc

        results.append(
            RoutingResult(
                draft_id=draft.draft_id,
                status=decision.status,
                net_total=priced.net_total,
                effective_discount_pct=priced.effective_discount_pct,
                approver_id=decision.approver_id,
                specialist_review_flags=decision.specialist_review_flags,
                duplicate_of=decision.duplicate_of,
            )
        )

    if output_path is not None:
        write_results(results, Path(output_path))
    return results


def write_results(results: Sequence[RoutingResult], path: Path) -> None:
    """Write JSON atomically so a crash never leaves a truncated file."""
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = [result.model_dump(mode="json") for result in results]

    handle, temp_name = tempfile.mkstemp(
        dir=str(path.parent), prefix=f".{path.name}.", suffix=".tmp"
    )
    try:
        with os.fdopen(handle, "w", encoding="utf-8") as stream:
            json.dump(payload, stream, indent=2)
            stream.write("\n")
        os.replace(temp_name, path)
    except BaseException:
        try:
            os.unlink(temp_name)
        except OSError:
            pass
        raise


def reset_database(path: Path) -> None:
    """Delete the local SQLite database and its WAL sidecars."""
    for candidate in (path, Path(f"{path}-wal"), Path(f"{path}-shm")):
        try:
            candidate.unlink()
        except FileNotFoundError:
            pass


def run(
    settings: Optional[Settings] = None,
    *,
    stock_client: Optional[StockClient] = None,
    store: Optional[QuoteStore] = None,
    drafts_path: Optional[Path] = None,
    output_path: Optional[Path] = None,
    reset: bool = False,
) -> List[RoutingResult]:
    """Full pipeline; the caller may inject a stock client and store."""
    settings = settings or get_settings()

    if reset:
        reset_database(settings.database_path)

    drafts = load_drafts(drafts_path or (settings.root / "draft_quotes.json"))
    reporting = load_reporting_data(settings.reporting_db_path)
    quote_store = store or QuoteStore(
        settings.database_path,
        specialist_role=settings.rules.roles.specialist,
    )

    owns_stock_client = stock_client is None
    if stock_client is None:
        stock_client = HttpStockClient(
            settings.stock_api_url, settings.rules.retry
        )

    try:
        return process_drafts(
            drafts,
            reporting,
            stock_client,
            quote_store,
            settings.rules,
            settings.now(),
            output_path=output_path or settings.output_path,
        )
    finally:
        if owns_stock_client:
            stock_client.close()


def main(argv: Optional[Sequence[str]] = None) -> int:
    parser = argparse.ArgumentParser(
        description="Process Ridgeline draft quotes for pricing, stock and approval routing."
    )
    parser.add_argument(
        "--fixture",
        action="store_true",
        help="use stock_fixture.json instead of the HTTP stock API (offline demo)",
    )
    parser.add_argument(
        "--reset",
        action="store_true",
        help="delete the local SQLite database before processing",
    )
    parser.add_argument(
        "--output",
        type=Path,
        default=None,
        help="path for routing_results.json (defaults to settings)",
    )
    args = parser.parse_args(argv)

    settings = get_settings()
    stock_client: Optional[StockClient] = None
    if args.fixture:
        stock_client = StaticStockClient.from_fixture(
            settings.root / "stock_fixture.json"
        )

    results = run(
        settings,
        stock_client=stock_client,
        output_path=args.output,
        reset=args.reset,
    )

    destination = args.output or settings.output_path
    print(f"Processed {len(results)} drafts -> {destination}")
    for result in results:
        approver = result.approver_id or "-"
        print(
            f"  {result.draft_id}  {result.status.value:18s} "
            f"approver={approver:4s} net=${result.net_total:,.2f}"
        )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
