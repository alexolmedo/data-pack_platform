"""End-to-end pipeline and the required routing_results.json output."""
import json
from decimal import Decimal
from pathlib import Path

import pytest

from app.models import QuoteStatus
from app.pipeline import PipelineError, load_drafts, process_drafts, run
from app.stock import StaticStockClient
from app.store import QuoteStore

ROOT = Path(__file__).resolve().parent.parent
FIXTURE = ROOT / "stock_fixture.json"


@pytest.fixture
def store(tmp_path):
    return QuoteStore(tmp_path / "pipeline.db")


def _process(store, drafts, reporting, settings, output_path, unavailable=()):
    stock = StaticStockClient.from_fixture(FIXTURE, unavailable=unavailable)
    return process_drafts(
        drafts,
        reporting,
        stock,
        store,
        settings.rules,
        settings.now(),
        output_path=output_path,
    )


def test_pipeline_produces_one_row_per_draft_in_order(
    store, drafts, reporting, settings, tmp_path
):
    output = tmp_path / "routing_results.json"
    results = _process(store, drafts, reporting, settings, output)

    assert [result.draft_id for result in results] == [
        draft.draft_id for draft in drafts
    ]
    assert output.exists()


def test_sample_routing_outcomes(store, drafts, reporting, settings, tmp_path):
    output = tmp_path / "routing_results.json"
    results = _process(store, drafts, reporting, settings, output)
    by_id = {result.draft_id: result for result in results}

    assert by_id["DQ-24101"].status is QuoteStatus.SPECIALIST_ONLY
    assert by_id["DQ-24101"].approver_id is None
    assert by_id["DQ-24101"].net_total == Decimal("435.23")

    assert by_id["DQ-24102"].status is QuoteStatus.APPROVER_REQUIRED
    assert by_id["DQ-24102"].approver_id == "U01"

    assert by_id["DQ-24103"].status is QuoteStatus.APPROVER_REQUIRED
    assert by_id["DQ-24103"].approver_id == "U05"
    assert by_id["DQ-24103"].effective_discount_pct == Decimal("26.22")

    assert by_id["DQ-24104"].approver_id == "U05"
    assert by_id["DQ-24105"].status is QuoteStatus.SPECIALIST_ONLY
    assert by_id["DQ-24106"].approver_id == "U04"  # Central OOO backup
    assert by_id["DQ-24107"].approver_id == "U01"
    assert by_id["DQ-24108"].status is QuoteStatus.SPECIALIST_ONLY

    assert by_id["DQ-24109"].approver_id == "U04"
    assert by_id["DQ-24110"].status is QuoteStatus.DUPLICATE
    assert by_id["DQ-24110"].duplicate_of == "DQ-24102"
    assert by_id["DQ-24110"].approver_id is None


def test_specialist_review_flags_flow_into_output(
    store, drafts, reporting, settings, tmp_path
):
    output = tmp_path / "routing_results.json"
    results = _process(store, drafts, reporting, settings, output)
    by_id = {result.draft_id: result for result in results}

    assert [
        flag.reason for flag in by_id["DQ-24105"].specialist_review_flags
    ] == ["extraction confidence 0.62"]
    assert "RL120374 not found" in by_id["DQ-24103"].specialist_review_flags[0].reason
    assert "49.5h old" in by_id["DQ-24104"].specialist_review_flags[0].reason
    assert "expired 2025-12-31" in by_id["DQ-24107"].specialist_review_flags[0].reason
    assert "out of stock in all warehouses" in (
        by_id["DQ-24108"].specialist_review_flags[0].reason
    )
    assert {flag.line for flag in by_id["DQ-24109"].specialist_review_flags} == {
        3,
        6,
    }
    assert by_id["DQ-24101"].specialist_review_flags == []
    assert by_id["DQ-24110"].specialist_review_flags == []


def test_json_file_has_exactly_the_required_shape(
    store, drafts, reporting, settings, tmp_path
):
    output = tmp_path / "routing_results.json"
    _process(store, drafts, reporting, settings, output)
    payload = json.loads(output.read_text(encoding="utf-8"))

    required = {
        "draft_id",
        "status",
        "net_total",
        "effective_discount_pct",
        "approver_id",
        "specialist_review_flags",
        "duplicate_of",
    }
    assert len(payload) == 10
    assert all(set(row) == required for row in payload)
    assert all(row["status"] in {"specialist_only", "approver_required", "duplicate"} for row in payload)

    first = payload[0]
    assert first["draft_id"] == "DQ-24101"
    assert first["net_total"] == pytest.approx(435.23)
    assert first["effective_discount_pct"] == pytest.approx(8.00)
    assert first["approver_id"] is None
    assert first["specialist_review_flags"] == []
    assert first["duplicate_of"] is None

    flagged = next(row for row in payload if row["draft_id"] == "DQ-24109")
    assert all(set(flag) == {"line", "reason"} for flag in flagged["specialist_review_flags"])

    duplicate = next(row for row in payload if row["draft_id"] == "DQ-24110")
    assert duplicate["status"] == "duplicate"
    assert duplicate["duplicate_of"] == "DQ-24102"
    assert duplicate["approver_id"] is None


def test_duplicate_gets_no_approval_request(
    store, drafts, reporting, settings, tmp_path
):
    _process(store, drafts, reporting, settings, tmp_path / "routing_results.json")

    assert store.get_approval_request("DQ-24110") is None
    requests = [
        store.get_approval_request(draft.draft_id) for draft in drafts
    ]
    assert sum(request is not None for request in requests) == 9
    assert len(store.list_audit()) == 10


def test_rerun_does_not_duplicate_audit_or_requests(
    store, drafts, reporting, settings, tmp_path
):
    output = tmp_path / "routing_results.json"
    _process(store, drafts, reporting, settings, output)
    first_payload = output.read_text(encoding="utf-8")

    second = _process(store, drafts, reporting, settings, output)

    assert len(second) == 10
    assert output.read_text(encoding="utf-8") == first_payload
    assert len(store.list_audit()) == 10
    assert all(
        len(store.list_audit(draft.draft_id)) == 1 for draft in drafts
    )


def test_stock_outage_is_visible_in_output(
    store, drafts, reporting, settings, tmp_path
):
    output = tmp_path / "routing_results.json"
    results = _process(
        store,
        drafts,
        reporting,
        settings,
        output,
        unavailable={"RL107944", "RL195448"},  # both lines of DQ-24104
    )
    by_id = {result.draft_id: result for result in results}

    quote_flags = [
        flag
        for flag in by_id["DQ-24104"].specialist_review_flags
        if flag.line is None
    ]
    assert len(quote_flags) == 1
    assert "stock unavailable after retries" in quote_flags[0].reason
    assert by_id["DQ-24104"].approver_id == "U05"


def test_load_drafts_rejects_duplicate_ids(tmp_path):
    payload = {
        "drafts": [
            {
                "draft_id": "DQ-X",
                "rfq_id": "RFQ-X",
                "customer_id": "C1001",
                "created_at": "2026-10-12T08:00:00-05:00",
                "prepared_by": "S01",
                "proposed_discount_pct": 5,
                "lines": [
                    {
                        "line": 1,
                        "part_number": "RL143309",
                        "quantity": 1,
                        "extraction_confidence": 0.99,
                    }
                ],
            }
        ]
    }
    payload["drafts"].append(payload["drafts"][0])
    path = tmp_path / "drafts.json"
    path.write_text(json.dumps(payload), encoding="utf-8")

    with pytest.raises(PipelineError, match="duplicate draft_id"):
        load_drafts(path)


def test_run_wires_settings_reporting_and_store(settings, tmp_path):
    store = QuoteStore(tmp_path / "run.db")
    stock = StaticStockClient.from_fixture(FIXTURE)
    output = tmp_path / "routing_results.json"

    results = run(settings, stock_client=stock, store=store, output_path=output)

    assert len(results) == 10
    assert output.exists()
    assert store.get_approval_request("DQ-24110") is None
