"""SQLite store: persistence, audit, approvals and duplicate detection."""
from datetime import timedelta
from decimal import Decimal

import pytest

from app.models import ApprovalRequestStatus, QuoteStatus
from app.pricing import price_quote
from app.routing import route_quote
from app.store import (
    ApprovalOrderError,
    NotAuthorized,
    QuoteAlreadyDecided,
    QuoteAlreadyExists,
    QuoteStore,
)


@pytest.fixture
def store(tmp_path):
    return QuoteStore(tmp_path / "quotes.db")


def _save(
    store,
    draft_id,
    drafts_by_id,
    reporting,
    settings,
    duplicate_of=None,
):
    draft = drafts_by_id[draft_id]
    priced = price_quote(draft, reporting, settings.rules, settings.now())
    decision = route_quote(
        draft,
        priced,
        reporting,
        settings.rules,
        settings.now(),
        duplicate_of=duplicate_of,
    )
    return store.save_quote(draft, priced, decision, settings.now())


def test_save_quote_persists_state_request_and_audit(
    store, drafts_by_id, reporting, settings
):
    record = _save(store, "DQ-24101", drafts_by_id, reporting, settings)

    assert record.status is QuoteStatus.SPECIALIST_ONLY
    assert record.net_total == Decimal("435.23")
    assert record.effective_discount_pct == Decimal("8.00")
    assert record.specialist_id == "S01"
    assert record.approver_id is None

    request = store.get_approval_request("DQ-24101")
    assert request is not None
    assert request.required_role == "quote_specialist"
    assert request.specialist_id == "S01"
    assert request.status is ApprovalRequestStatus.PENDING

    audit = store.list_audit("DQ-24101")
    assert len(audit) == 1
    assert audit[0].action == "routed"
    assert audit[0].status_before == "received"
    assert audit[0].status_after == "specialist_only"
    assert audit[0].after["net_total"] == "435.23"


def test_save_quote_creates_routed_manager_request(
    store, drafts_by_id, reporting, settings
):
    _save(store, "DQ-24102", drafts_by_id, reporting, settings)

    request = store.get_approval_request("DQ-24102")
    assert request is not None
    assert request.required_role == "regional_manager"
    assert request.approver_id == "U01"
    assert request.specialist_id == "S02"


def test_saving_the_same_draft_twice_is_rejected(
    store, drafts_by_id, reporting, settings
):
    _save(store, "DQ-24101", drafts_by_id, reporting, settings)

    with pytest.raises(QuoteAlreadyExists):
        _save(store, "DQ-24101", drafts_by_id, reporting, settings)


def test_duplicate_detection_matches_rfq_and_lines(
    store, drafts_by_id, reporting, settings
):
    _save(store, "DQ-24102", drafts_by_id, reporting, settings)

    original = store.find_duplicate(
        drafts_by_id["DQ-24110"],
        settings.now(),
        settings.rules.thresholds.duplicate_window_hours,
    )

    assert original == "DQ-24102"


def test_duplicate_quote_creates_no_approval_request(
    store, drafts_by_id, reporting, settings
):
    _save(store, "DQ-24102", drafts_by_id, reporting, settings)
    record = _save(
        store,
        "DQ-24110",
        drafts_by_id,
        reporting,
        settings,
        duplicate_of="DQ-24102",
    )

    assert record.status is QuoteStatus.DUPLICATE
    assert record.duplicate_of == "DQ-24102"
    assert store.get_approval_request("DQ-24110") is None
    assert store.get_approval_request("DQ-24102") is not None
    assert [event.action for event in store.list_audit("DQ-24110")] == [
        "marked_duplicate"
    ]


def test_duplicate_window_is_24_hours(store, drafts_by_id, reporting, settings):
    _save(store, "DQ-24102", drafts_by_id, reporting, settings)
    later = settings.now() + timedelta(hours=25)

    assert (
        store.find_duplicate(
            drafts_by_id["DQ-24110"], later, 24
        )
        is None
    )


def test_duplicate_requires_identical_lines(
    store, drafts_by_id, reporting, settings
):
    _save(store, "DQ-24102", drafts_by_id, reporting, settings)
    changed = drafts_by_id["DQ-24110"].model_copy(deep=True)
    changed.lines[0].quantity = Decimal("999")

    assert store.find_duplicate(changed, settings.now(), 24) is None


def test_duplicate_requires_the_same_rfq(
    store, drafts_by_id, reporting, settings
):
    _save(store, "DQ-24102", drafts_by_id, reporting, settings)
    changed = drafts_by_id["DQ-24110"].model_copy(update={"rfq_id": "RFQ-OTHER"})

    assert store.find_duplicate(changed, settings.now(), 24) is None


def test_specialist_approves_specialist_only_quote(
    store, drafts_by_id, reporting, settings
):
    _save(store, "DQ-24101", drafts_by_id, reporting, settings)

    record = store.approve(
        "DQ-24101", "S01", "quote_specialist", settings.now(), note="looks good"
    )

    assert record.status is QuoteStatus.APPROVED
    request = store.get_approval_request("DQ-24101")
    assert request is not None
    assert request.status is ApprovalRequestStatus.APPROVED
    assert request.specialist_approved_by == "S01"
    assert request.decided_by == "S01"
    assert request.note == "looks good"
    assert [event.action for event in store.list_audit("DQ-24101")] == [
        "routed",
        "specialist_approved",
        "approved",
    ]

    with pytest.raises(QuoteAlreadyDecided):
        store.approve("DQ-24101", "S01", "quote_specialist", settings.now())


def test_manager_cannot_approve_before_specialist_signoff(
    store, drafts_by_id, reporting, settings
):
    _save(store, "DQ-24102", drafts_by_id, reporting, settings)

    with pytest.raises(ApprovalOrderError):
        store.approve(
            "DQ-24102", "U01", "regional_manager", settings.now()
        )

    record = store.approve(
        "DQ-24102", "S02", "quote_specialist", settings.now()
    )
    assert record.status is QuoteStatus.APPROVER_REQUIRED
    request = store.get_approval_request("DQ-24102")
    assert request is not None
    assert request.specialist_approved_at is not None
    assert request.decided_at is None

    record = store.approve(
        "DQ-24102", "U01", "regional_manager", settings.now()
    )
    assert record.status is QuoteStatus.APPROVED
    assert [event.action for event in store.list_audit("DQ-24102")] == [
        "routed",
        "specialist_approved",
        "approved",
    ]

    with pytest.raises(QuoteAlreadyDecided):
        store.approve("DQ-24102", "U01", "regional_manager", settings.now())


def test_only_a_specialist_or_routed_approver_can_approve(
    store, drafts_by_id, reporting, settings
):
    _save(store, "DQ-24101", drafts_by_id, reporting, settings)

    with pytest.raises(NotAuthorized):
        store.approve("DQ-24101", "U01", "regional_manager", settings.now())

    # Any specialist may give the specialist sign-off.
    record = store.approve(
        "DQ-24101", "S03", "quote_specialist", settings.now()
    )
    assert record.status is QuoteStatus.APPROVED


def test_reject_records_decision_and_audit(
    store, drafts_by_id, reporting, settings
):
    _save(store, "DQ-24102", drafts_by_id, reporting, settings)

    record = store.reject(
        "DQ-24102", "U01", "regional_manager", settings.now(), note="below floor"
    )

    assert record.status is QuoteStatus.REJECTED
    request = store.get_approval_request("DQ-24102")
    assert request is not None
    assert request.status is ApprovalRequestStatus.REJECTED
    assert request.decided_by == "U01"
    last = store.list_audit("DQ-24102")[-1]
    assert last.action == "rejected"
    assert last.status_before == "approver_required"
    assert last.status_after == "rejected"


def test_duplicate_cannot_be_approved_or_rejected(
    store, drafts_by_id, reporting, settings
):
    _save(store, "DQ-24102", drafts_by_id, reporting, settings)
    _save(
        store,
        "DQ-24110",
        drafts_by_id,
        reporting,
        settings,
        duplicate_of="DQ-24102",
    )

    with pytest.raises(QuoteAlreadyDecided):
        store.reject("DQ-24110", "S02", "quote_specialist", settings.now())


def test_queues_reflect_specialist_and_approver_stages(
    store, drafts_by_id, reporting, settings
):
    _save(store, "DQ-24101", drafts_by_id, reporting, settings)
    _save(store, "DQ-24102", drafts_by_id, reporting, settings)

    assert {quote.draft_id for quote in store.list_pending_specialist()} == {
        "DQ-24101",
        "DQ-24102",
    }
    assert store.list_pending_approvals("U01") == []

    store.approve("DQ-24102", "S02", "quote_specialist", settings.now())

    assert {quote.draft_id for quote in store.list_pending_specialist()} == {
        "DQ-24101"
    }
    assert {quote.draft_id for quote in store.list_pending_approvals("U01")} == {
        "DQ-24102"
    }

    store.approve("DQ-24102", "U01", "regional_manager", settings.now())

    assert store.list_pending_approvals("U01") == []
    assert {quote.draft_id for quote in store.list_quotes(QuoteStatus.APPROVED)} == {
        "DQ-24102"
    }


def test_record_edit_preserves_before_and_after(
    store, drafts_by_id, reporting, settings
):
    _save(store, "DQ-24101", drafts_by_id, reporting, settings)

    store.record_edit(
        "DQ-24101",
        "S01",
        before={"line": 1, "quantity": 30},
        after={"line": 1, "quantity": 25},
        now=settings.now(),
        note="customer changed quantity",
    )

    event = store.list_audit("DQ-24101")[-1]
    assert event.action == "edited"
    assert event.status_before == event.status_after == "specialist_only"
    assert event.before == {"line": 1, "quantity": 30}
    assert event.after == {
        "line": 1,
        "quantity": 25,
        "note": "customer changed quantity",
    }
