"""Smoke tests for the configuration and domain-model foundation."""
import json
from datetime import date
from decimal import Decimal
from pathlib import Path

from app.config import load_settings
from app.models import (
    Approver,
    DraftQuote,
    QuoteStatus,
    StockStatus,
)

ROOT = Path(__file__).resolve().parent.parent


def test_rules_load_from_yaml():
    settings = load_settings(env={}, root=ROOT)

    thresholds = settings.rules.thresholds
    assert thresholds.specialist_max_discount_pct == Decimal("15")
    assert thresholds.manager_max_discount_pct == Decimal("25")
    assert thresholds.manager_net_total_threshold_usd == Decimal("25000")
    assert thresholds.low_confidence_threshold == Decimal("0.85")
    assert thresholds.stock_stale_hours == 24
    assert thresholds.reporting_stale_hours == 36


def test_policy_clock_is_timezone_aware():
    settings = load_settings(env={}, root=ROOT)
    now = settings.now()

    assert now.tzinfo is not None
    assert now.date().isoformat() == "2026-10-12"
    assert now.utcoffset().total_seconds() == -5 * 3600


def test_policy_now_env_override_wins():
    settings = load_settings(
        env={"POLICY_NOW": "2026-10-13T09:30:00-05:00"}, root=ROOT
    )
    assert settings.now().date().isoformat() == "2026-10-13"


def test_all_sample_drafts_parse():
    payload = json.loads((ROOT / "draft_quotes.json").read_text(encoding="utf-8"))
    drafts = [DraftQuote.model_validate(item) for item in payload["drafts"]]

    assert len(drafts) == 10
    assert drafts[0].draft_id == "DQ-24101"
    assert drafts[0].lines[0].quantity == Decimal("30")
    assert drafts[0].lines[0].extraction_confidence == Decimal("0.97")


def test_approver_out_of_office_is_inclusive():
    approver = Approver(
        approver_id="U02",
        name="Tom Becker",
        role="regional_manager",
        region="Central",
        backup_approver_id="U04",
        out_of_office_from="2026-10-12",
        out_of_office_to="2026-10-16",
    )
    assert approver.is_out_of_office(date(2026, 10, 12))
    assert approver.is_out_of_office(date(2026, 10, 16))
    assert not approver.is_out_of_office(date(2026, 10, 17))
    assert approver.covers_region("Central")


def test_output_status_values_match_deliverable():
    assert QuoteStatus.SPECIALIST_ONLY.value == "specialist_only"
    assert QuoteStatus.APPROVER_REQUIRED.value == "approver_required"
    assert QuoteStatus.DUPLICATE.value == "duplicate"
    assert StockStatus.UNAVAILABLE.value == "unavailable"
