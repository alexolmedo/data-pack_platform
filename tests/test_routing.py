"""Approval routing: every tier, the backup chain and specialist flags."""
from dataclasses import replace
from datetime import date, datetime
from decimal import Decimal

from app.models import (
    DraftQuote,
    PricedQuote,
    QuoteStatus,
    SpecialistFlag,
)
from app.pricing import price_quote
from app.routing import route_quote

TODAY = date(2026, 10, 12)


def _synthetic_draft(prepared_by="S01", lines=None):
    return DraftQuote(
        draft_id="DQ-TEST",
        rfq_id="RFQ-TEST",
        customer_id="C1005",
        created_at=datetime(2026, 10, 12, 9, 0),
        prepared_by=prepared_by,
        proposed_discount_pct=Decimal("5"),
        lines=lines or [],
    )


def _synthetic_priced(
    settings,
    *,
    effective_discount="5",
    net_total="1000",
    region="East",
    customer="C1005",
    flags=None,
):
    return PricedQuote(
        draft_id="DQ-TEST",
        rfq_id="RFQ-TEST",
        customer_id=customer,
        customer_region=region,
        proposed_discount_pct=Decimal("5"),
        lines=[],
        net_total=Decimal(net_total),
        list_total=Decimal(net_total),
        effective_discount_pct=Decimal(effective_discount),
        specialist_review_flags=flags or [],
        computed_at=settings.now(),
    )


def _route(draft, priced, reporting, settings, **kwargs):
    return route_quote(
        draft, priced, reporting, settings.rules, settings.now(), **kwargs
    )


def _route_real(draft_id, drafts_by_id, reporting, settings, **kwargs):
    draft = drafts_by_id[draft_id]
    priced = price_quote(draft, reporting, settings.rules, settings.now())
    return _route(draft, priced, reporting, settings, **kwargs)


def test_specialist_only_tier(drafts_by_id, reporting, settings):
    decision = _route_real("DQ-24101", drafts_by_id, reporting, settings)

    assert decision.status is QuoteStatus.SPECIALIST_ONLY
    assert decision.required_role == "quote_specialist"
    assert decision.approver_id is None
    assert decision.specialist_id == "S01"
    assert decision.specialist_review_flags == []


def test_manager_tier_by_discount(drafts_by_id, reporting, settings):
    decision = _route_real("DQ-24102", drafts_by_id, reporting, settings)

    assert decision.status is QuoteStatus.APPROVER_REQUIRED
    assert decision.required_role == "regional_manager"
    assert decision.approver_id == "U01"  # West
    assert decision.specialist_id == "S02"  # prepared_by


def test_net_total_triggers_manager_even_with_small_discount(
    drafts_by_id, reporting, settings
):
    decision = _route_real("DQ-24106", drafts_by_id, reporting, settings)

    assert decision.status is QuoteStatus.APPROVER_REQUIRED
    # Central manager U02 is out of office this week; route to the backup.
    assert decision.approver_id == "U04"
    assert "out of office; backup U04" in decision.rationale
    assert "net total $47,652.00" in decision.rationale


def test_vp_tier(drafts_by_id, reporting, settings):
    decision = _route_real("DQ-24104", drafts_by_id, reporting, settings)

    assert decision.status is QuoteStatus.APPROVER_REQUIRED
    assert decision.required_role == "vp_sales"
    assert decision.approver_id == "U05"


def test_contract_pricing_can_move_a_quote_to_the_vp_tier(
    drafts_by_id, reporting, settings
):
    decision = _route_real("DQ-24103", drafts_by_id, reporting, settings)

    assert decision.status is QuoteStatus.APPROVER_REQUIRED
    assert decision.approver_id == "U05"
    assert "26.22%" in decision.rationale


def test_boundary_15_percent_is_specialist_only(reporting, settings):
    decision = _route(
        _synthetic_draft(),
        _synthetic_priced(settings, effective_discount="15.00", net_total="24000"),
        reporting,
        settings,
    )

    assert decision.status is QuoteStatus.SPECIALIST_ONLY
    assert decision.approver_id is None


def test_boundary_above_15_percent_is_manager(reporting, settings):
    decision = _route(
        _synthetic_draft(),
        _synthetic_priced(settings, effective_discount="15.01", net_total="24000"),
        reporting,
        settings,
    )

    assert decision.status is QuoteStatus.APPROVER_REQUIRED
    assert decision.approver_id == "U03"  # East


def test_net_total_at_threshold_is_manager(reporting, settings):
    decision = _route(
        _synthetic_draft(),
        _synthetic_priced(settings, effective_discount="5", net_total="25000"),
        reporting,
        settings,
    )

    assert decision.status is QuoteStatus.APPROVER_REQUIRED
    assert decision.approver_id == "U03"


def test_boundary_25_percent_is_still_manager(reporting, settings):
    decision = _route(
        _synthetic_draft(),
        _synthetic_priced(settings, effective_discount="25.00", net_total="1000"),
        reporting,
        settings,
    )

    assert decision.status is QuoteStatus.APPROVER_REQUIRED
    assert decision.required_role == "regional_manager"
    assert decision.approver_id == "U03"


def test_above_25_percent_is_vp(reporting, settings):
    decision = _route(
        _synthetic_draft(),
        _synthetic_priced(settings, effective_discount="25.01", net_total="1000"),
        reporting,
        settings,
    )

    assert decision.status is QuoteStatus.APPROVER_REQUIRED
    assert decision.required_role == "vp_sales"
    assert decision.approver_id == "U05"


def test_low_confidence_flags_are_added_for_manager_quote(
    drafts_by_id, reporting, settings
):
    decision = _route_real("DQ-24109", drafts_by_id, reporting, settings)

    assert decision.status is QuoteStatus.APPROVER_REQUIRED
    assert decision.approver_id == "U04"  # Central backup
    reasons = [flag.reason for flag in decision.specialist_review_flags]
    assert "extraction confidence 0.79" in reasons
    assert any(flag.line == 3 for flag in decision.specialist_review_flags)


def test_low_confidence_quote_can_still_be_specialist_only(
    drafts_by_id, reporting, settings
):
    decision = _route_real("DQ-24105", drafts_by_id, reporting, settings)

    assert decision.status is QuoteStatus.SPECIALIST_ONLY
    assert decision.specialist_id == "S02"  # prepared_by
    assert [flag.reason for flag in decision.specialist_review_flags] == [
        "extraction confidence 0.62"
    ]


def test_extra_flags_are_included_and_deduplicated(
    drafts_by_id, reporting, settings
):
    stock_flag = SpecialistFlag(
        line=1, reason="stock reading is 25.0h old (limit 24h); confirm before sending"
    )

    decision = _route_real(
        "DQ-24101",
        drafts_by_id,
        reporting,
        settings,
        extra_flags=[stock_flag, stock_flag],
    )

    matching = [
        flag
        for flag in decision.specialist_review_flags
        if flag.reason == stock_flag.reason
    ]
    assert len(matching) == 1


def test_central_ooo_chain_can_fall_through_to_the_vp(
    drafts_by_id, reporting, settings
):
    modified = dict(reporting.approvers)
    modified["U04"] = modified["U04"].model_copy(
        update={
            "out_of_office_from": date(2026, 10, 12),
            "out_of_office_to": date(2026, 10, 16),
        }
    )
    modified_reporting = replace(reporting, approvers=modified)
    draft = drafts_by_id["DQ-24106"]
    priced = price_quote(draft, modified_reporting, settings.rules, settings.now())

    decision = _route(draft, priced, modified_reporting, settings)

    assert decision.approver_id == "U05"
    assert "U02 out of office; backup U05" in decision.rationale


def test_specialist_backup_is_used_when_prepared_by_is_out(
    drafts_by_id, reporting, settings
):
    modified = dict(reporting.approvers)
    modified["S01"] = modified["S01"].model_copy(
        update={
            "out_of_office_from": date(2026, 10, 12),
            "out_of_office_to": date(2026, 10, 16),
        }
    )
    modified_reporting = replace(reporting, approvers=modified)

    decision = _route_real(
        "DQ-24101", drafts_by_id, modified_reporting, settings
    )

    assert decision.specialist_id == "S02"


def test_no_specialist_available_is_flagged(
    drafts_by_id, reporting, settings
):
    non_specialists = {
        key: value
        for key, value in reporting.approvers.items()
        if value.role != "quote_specialist"
    }
    modified_reporting = replace(reporting, approvers=non_specialists)
    draft = drafts_by_id["DQ-24101"]
    priced = price_quote(draft, modified_reporting, settings.rules, settings.now())

    decision = _route(draft, priced, modified_reporting, settings)

    assert decision.specialist_id is None
    reasons = [flag.reason for flag in decision.specialist_review_flags]
    assert "no quote specialist available; assign manually" in reasons


def test_missing_regional_manager_is_flagged(reporting, settings):
    without_east = {
        key: value
        for key, value in reporting.approvers.items()
        if key != "U03"
    }
    modified_reporting = replace(reporting, approvers=without_east)

    decision = _route(
        _synthetic_draft(),
        _synthetic_priced(settings, effective_discount="18", net_total="1000"),
        modified_reporting,
        settings,
    )

    assert decision.status is QuoteStatus.APPROVER_REQUIRED
    assert decision.approver_id is None
    reasons = [flag.reason for flag in decision.specialist_review_flags]
    assert any("no regional_manager approver configured" in reason for reason in reasons)


def test_duplicate_does_not_create_an_approval_request(
    drafts_by_id, reporting, settings
):
    decision = _route_real(
        "DQ-24110",
        drafts_by_id,
        reporting,
        settings,
        duplicate_of="DQ-24102",
    )

    assert decision.status is QuoteStatus.DUPLICATE
    assert decision.duplicate_of == "DQ-24102"
    assert decision.approver_id is None
    assert decision.specialist_id is None
    assert "no new approval request" in decision.rationale
