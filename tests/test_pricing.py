"""Pricing rules: contract selection, expired contracts, effective discount."""
from datetime import date, datetime, timedelta
from decimal import Decimal

import pytest

from app.models import ContractPrice, DraftLine, DraftQuote, PriceSource
from app.pricing import PricingError, price_quote
from app.reporting import is_reporting_stale, reporting_age_hours


def _price(draft, reporting, settings):
    return price_quote(draft, reporting, settings.rules, settings.now())


def test_draft_without_contract_uses_list_price(drafts_by_id, reporting, settings):
    priced = _price(drafts_by_id["DQ-24101"], reporting, settings)

    assert priced.net_total == Decimal("435.23")
    assert priced.list_total == Decimal("473.08")
    assert priced.effective_discount_pct == Decimal("8.00")
    assert all(line.price_source is PriceSource.LIST for line in priced.lines)
    assert priced.specialist_review_flags == []


def test_valid_contract_price_is_applied(drafts_by_id, reporting):
    contract = reporting.contracts.valid_on("C1001", "RL143309", date(2026, 10, 12))
    assert contract is not None
    assert contract.contract_price == Decimal("2.54")


def test_effective_discount_can_exceed_proposed_discount(
    drafts_by_id, reporting, settings
):
    # DQ-24103 proposes 22%, but contract prices below list make the
    # list-price-relative discount 26.22% - a VP tier, not a manager tier.
    priced = _price(drafts_by_id["DQ-24103"], reporting, settings)

    assert priced.proposed_discount_pct == Decimal("22")
    assert priced.effective_discount_pct == Decimal("26.22")
    assert priced.net_total == Decimal("728.80")
    assert priced.list_total == Decimal("987.82")


def test_net_total_threshold_case_prices_at_list(drafts_by_id, reporting, settings):
    priced = _price(drafts_by_id["DQ-24106"], reporting, settings)

    assert priced.net_total == Decimal("47652.00")
    assert priced.list_total == Decimal("50160.00")
    assert priced.effective_discount_pct == Decimal("5.00")


def test_expired_contract_uses_list_price_and_flags_line(
    drafts_by_id, reporting, settings
):
    priced = _price(drafts_by_id["DQ-24107"], reporting, settings)
    line_one = priced.lines[0]

    assert line_one.part_number == "RL171482"
    assert line_one.price_source is PriceSource.EXPIRED_CONTRACT
    assert line_one.unit_price == line_one.list_price == Decimal("8.40")
    assert len(line_one.flags) == 1
    assert line_one.flags[0].line == 1
    assert "expired 2025-12-31" in line_one.flags[0].reason
    assert line_one.flags[0] in priced.specialist_review_flags


def test_missing_product_raises_instead_of_pricing_at_zero(reporting, settings):
    draft = DraftQuote(
        draft_id="DQ-TEST",
        rfq_id="RFQ-TEST",
        customer_id="C1001",
        created_at=datetime(2026, 10, 12, 9, 0),
        prepared_by="S01",
        proposed_discount_pct=Decimal("5"),
        lines=[
            DraftLine(
                line=1,
                part_number="DOES-NOT-EXIST",
                quantity=Decimal("1"),
                extraction_confidence=Decimal("0.99"),
            )
        ],
    )

    with pytest.raises(PricingError, match="not found in reporting database"):
        price_quote(draft, reporting, settings.rules, settings.now())


def test_reporting_export_is_fresh_at_policy_time(reporting, settings):
    now = settings.now()
    age = reporting_age_hours(reporting.exported_at, now)

    assert age == pytest.approx(31.5)
    assert not is_reporting_stale(reporting.exported_at, now, 36)


def test_reporting_export_becomes_stale_after_limit(reporting, settings):
    later = settings.now() + timedelta(hours=6)
    assert is_reporting_stale(reporting.exported_at, later, 36)


def test_missing_export_timestamp_counts_as_stale(settings):
    assert is_reporting_stale(None, settings.now(), 36)


def test_contract_book_prefers_newest_valid_contract():
    from app.reporting import ContractBook

    book = ContractBook.from_prices(
        [
            ContractPrice(
                customer_id="C1",
                part_number="P1",
                contract_price=Decimal("10.00"),
                valid_from=date(2025, 1, 1),
                valid_to=date(2025, 12, 31),
            ),
            ContractPrice(
                customer_id="C1",
                part_number="P1",
                contract_price=Decimal("9.00"),
                valid_from=date(2026, 1, 1),
                valid_to=date(2026, 12, 31),
            ),
        ]
    )

    chosen = book.valid_on("C1", "P1", date(2026, 10, 12))
    assert chosen is not None
    assert chosen.contract_price == Decimal("9.00")
