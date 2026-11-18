"""Pure pricing: contract/list selection and effective discount.

No network or database access happens here, which keeps the rules fast to
test. Stock checks are layered on in ``app.stock``.
"""
from datetime import date, datetime
from decimal import Decimal, ROUND_HALF_UP
from typing import List, Optional, Tuple

from app.config import ApprovalRules
from app.models import (
    ContractPrice,
    DraftQuote,
    PriceSource,
    PricedLine,
    PricedQuote,
    Product,
    SpecialistFlag,
)
from app.reporting import ReportingData, is_reporting_stale, reporting_age_hours

CENT = Decimal("0.01")
ONE = Decimal("1")
HUNDRED = Decimal("100")


class PricingError(Exception):
    """A draft cannot be priced from the available reporting data."""


def money(value: Decimal) -> Decimal:
    return value.quantize(CENT, rounding=ROUND_HALF_UP)


def percent(value: Decimal) -> Decimal:
    return value.quantize(CENT, rounding=ROUND_HALF_UP)


def _select_price(
    product: Product,
    line_number: int,
    valid_contract: Optional[ContractPrice],
    most_recent_contract: Optional[ContractPrice],
    today: date,
) -> Tuple[Decimal, PriceSource, List[SpecialistFlag]]:
    """Apply policy rules 3 and 4 for a single line."""
    if valid_contract is not None:
        return valid_contract.contract_price, PriceSource.CONTRACT, []

    if most_recent_contract is not None and most_recent_contract.valid_to < today:
        return product.list_price, PriceSource.EXPIRED_CONTRACT, [
            SpecialistFlag(
                line=line_number,
                reason=(
                    "contract price expired "
                    f"{most_recent_contract.valid_to.isoformat()}; used list price"
                ),
            )
        ]

    # No contract, or a future-dated one that is not effective yet.
    return product.list_price, PriceSource.LIST, []


def price_quote(
    draft: DraftQuote,
    reporting: ReportingData,
    rules: ApprovalRules,
    now: datetime,
) -> PricedQuote:
    """Price every line and compute net total and effective discount.

    The proposed discount is applied on top of the chosen unit price. The
    effective discount is measured against list price, as the policy requires.
    """
    customer = reporting.customer(draft.customer_id)
    if customer is None:
        raise PricingError(
            f"{draft.draft_id}: unknown customer {draft.customer_id!r}"
        )

    if not (Decimal("0") <= draft.proposed_discount_pct < HUNDRED):
        raise PricingError(
            f"{draft.draft_id}: proposed_discount_pct "
            f"{draft.proposed_discount_pct} is outside 0..100"
        )

    today = now.date()
    discount_factor = ONE - (draft.proposed_discount_pct / HUNDRED)

    priced_lines: List[PricedLine] = []
    quote_flags: List[SpecialistFlag] = []
    net_raw = Decimal("0")
    list_raw = Decimal("0")

    for line in draft.lines:
        product = reporting.product(line.part_number)
        if product is None:
            raise PricingError(
                f"{draft.draft_id} line {line.line}: part "
                f"{line.part_number!r} not found in reporting database"
            )

        valid_contract = reporting.contracts.valid_on(
            draft.customer_id, line.part_number, today
        )
        most_recent_contract = reporting.contracts.most_recent(
            draft.customer_id, line.part_number
        )
        unit_price, source, pricing_flags = _select_price(
            product, line.line, valid_contract, most_recent_contract, today
        )
        quote_flags.extend(pricing_flags)

        net_raw += line.quantity * unit_price * discount_factor
        list_raw += line.quantity * product.list_price

        priced_lines.append(
            PricedLine(
                line=line.line,
                part_number=line.part_number,
                quantity=line.quantity,
                unit_price=unit_price,
                list_price=product.list_price,
                price_source=source,
                flags=pricing_flags,
            )
        )

    net_total = money(net_raw)
    list_total = money(list_raw)
    effective_discount = (
        percent((ONE - (net_total / list_total)) * HUNDRED)
        if list_total > 0
        else Decimal("0.00")
    )

    if is_reporting_stale(
        reporting.exported_at, now, rules.thresholds.reporting_stale_hours
    ):
        age = reporting_age_hours(reporting.exported_at, now)
        limit = rules.thresholds.reporting_stale_hours
        reason = (
            f"reporting export timestamp missing; data must be confirmed"
            if age is None
            else f"reporting export is {age:.1f}h old (limit {limit}h)"
        )
        quote_flags.append(SpecialistFlag(line=None, reason=reason))

    return PricedQuote(
        draft_id=draft.draft_id,
        rfq_id=draft.rfq_id,
        customer_id=customer.customer_id,
        customer_region=customer.region,
        proposed_discount_pct=draft.proposed_discount_pct,
        lines=priced_lines,
        net_total=net_total,
        list_total=list_total,
        effective_discount_pct=effective_discount,
        reporting_exported_at=reporting.exported_at,
        specialist_review_flags=quote_flags,
        computed_at=now,
    )


def price_drafts(
    drafts: List[DraftQuote],
    reporting: ReportingData,
    rules: ApprovalRules,
    now: datetime,
) -> List[PricedQuote]:
    return [price_quote(draft, reporting, rules, now) for draft in drafts]
