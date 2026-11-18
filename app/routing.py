"""Approval routing policy, implemented as pure functions over config data.

Rules live in ``config/approval_rules.yml``; approver identities, regions,
out-of-office dates and backups come from the read-only reporting export.
"""
from datetime import date, datetime
from decimal import Decimal
from typing import Dict, Iterable, List, Optional, Sequence, Tuple

from app.config import ApprovalRules
from app.models import (
    Approver,
    DraftQuote,
    PricedQuote,
    QuoteStatus,
    RoutingDecision,
    SpecialistFlag,
)
from app.reporting import ReportingData


def _merge_flags(*groups: Iterable[SpecialistFlag]) -> List[SpecialistFlag]:
    """Concatenate flag groups, dropping exact duplicates, preserving order."""
    merged: List[SpecialistFlag] = []
    seen = set()
    for group in groups:
        for flag in group:
            key = (flag.line, flag.reason)
            if key in seen:
                continue
            seen.add(key)
            merged.append(flag)
    return merged


def confidence_flags(draft: DraftQuote, rules: ApprovalRules) -> List[SpecialistFlag]:
    """Policy rule 8: low-confidence extraction lines need a specialist."""
    threshold = rules.thresholds.low_confidence_threshold
    flags: List[SpecialistFlag] = []
    for line in draft.lines:
        confidence = line.extraction_confidence
        if confidence is None:
            flags.append(
                SpecialistFlag(
                    line=line.line,
                    reason="extraction confidence missing",
                )
            )
        elif confidence < threshold:
            flags.append(
                SpecialistFlag(
                    line=line.line,
                    reason=f"extraction confidence {float(confidence):.2f}",
                )
            )
    return flags


def _find_role_approver(
    reporting: ReportingData, role: str, region: str
) -> Optional[Approver]:
    matches = [
        approver
        for approver in reporting.approvers_with_role(role)
        if approver.covers_region(region)
    ]
    if not matches:
        return None
    # Prefer an exact regional match over an "ALL" fallback.
    matches.sort(key=lambda approver: (approver.region != region, approver.approver_id))
    return matches[0]


def _resolve_with_backup(
    start: Approver, reporting: ReportingData, today: date
) -> Tuple[Optional[Approver], List[str]]:
    """Follow the backup chain while an approver is out of office.

    Returns the available approver (or None) and the chain of IDs visited.
    """
    chain: List[str] = []
    seen = set()
    current: Optional[Approver] = start

    while current is not None and current.approver_id not in seen:
        seen.add(current.approver_id)
        chain.append(current.approver_id)
        if not current.is_out_of_office(today):
            return current, chain
        backup_id = current.backup_approver_id
        if not backup_id:
            return None, chain
        current = reporting.approver(backup_id)

    return None, chain


def _resolve_specialist(
    draft: DraftQuote,
    reporting: ReportingData,
    rules: ApprovalRules,
    today: date,
) -> Optional[Approver]:
    role = rules.roles.specialist
    specialists = reporting.approvers_with_role(role)
    by_id: Dict[str, Approver] = {item.approver_id: item for item in specialists}

    preferred = None
    if rules.specialist.assignment == "prepared_by" and draft.prepared_by:
        preferred = by_id.get(draft.prepared_by)

    if preferred is not None:
        seen = set()
        current: Optional[Approver] = preferred
        while current is not None and current.approver_id not in seen:
            seen.add(current.approver_id)
            if not current.is_out_of_office(today):
                return current
            backup_id = current.backup_approver_id
            current = by_id.get(backup_id) if backup_id else None

    for specialist in specialists:
        if not specialist.is_out_of_office(today):
            return specialist
    return None


def _required_tier(priced: PricedQuote, rules: ApprovalRules) -> str:
    thresholds = rules.thresholds
    if priced.effective_discount_pct > thresholds.manager_max_discount_pct:
        return "vp"
    if (
        priced.effective_discount_pct > thresholds.specialist_max_discount_pct
        or priced.net_total >= thresholds.manager_net_total_threshold_usd
    ):
        return "manager"
    return "specialist"


def _money(value: Decimal) -> str:
    return f"${value:,.2f}"


def route_quote(
    draft: DraftQuote,
    priced: PricedQuote,
    reporting: ReportingData,
    rules: ApprovalRules,
    now: datetime,
    *,
    extra_flags: Iterable[SpecialistFlag] = (),
    duplicate_of: Optional[str] = None,
) -> RoutingDecision:
    """Decide who must approve, applying the configured thresholds and backups.

    ``extra_flags`` carries outcomes from the stock layer so the routing
    decision owns the final, complete specialist-review flag list.
    """
    today = now.date()
    flags = _merge_flags(
        priced.specialist_review_flags,
        confidence_flags(draft, rules),
        extra_flags,
    )

    if duplicate_of:
        return RoutingDecision(
            draft_id=draft.draft_id,
            status=QuoteStatus.DUPLICATE,
            required_role=None,
            approver_id=None,
            specialist_id=None,
            specialist_review_flags=flags,
            duplicate_of=duplicate_of,
            rationale=f"duplicate of {duplicate_of}; no new approval request",
        )

    specialist = _resolve_specialist(draft, reporting, rules, today)
    if specialist is None:
        flags.append(
            SpecialistFlag(
                line=None,
                reason="no quote specialist available; assign manually",
            )
        )

    tier = _required_tier(priced, rules)
    if tier == "specialist":
        return RoutingDecision(
            draft_id=draft.draft_id,
            status=QuoteStatus.SPECIALIST_ONLY,
            required_role=rules.roles.specialist,
            approver_id=None,
            specialist_id=specialist.approver_id if specialist else None,
            specialist_review_flags=flags,
            rationale=(
                f"effective discount {priced.effective_discount_pct:.2f}% "
                f"and net total {_money(priced.net_total)} within specialist threshold"
            ),
        )

    role = rules.roles.vp if tier == "vp" else rules.roles.regional_manager
    base = _find_role_approver(reporting, role, priced.customer_region)
    region = priced.customer_region

    if base is None:
        flags.append(
            SpecialistFlag(
                line=None,
                reason=(
                    f"no {role} approver configured for region {region}; "
                    "assign manually"
                ),
            )
        )
        return RoutingDecision(
            draft_id=draft.draft_id,
            status=QuoteStatus.APPROVER_REQUIRED,
            required_role=role,
            approver_id=None,
            specialist_id=specialist.approver_id if specialist else None,
            specialist_review_flags=flags,
            rationale=(
                f"{role} approval required but no approver covers region {region}"
            ),
        )

    resolved, chain = _resolve_with_backup(base, reporting, today)
    if resolved is None:
        flags.append(
            SpecialistFlag(
                line=None,
                reason=(
                    f"required approver {base.approver_id} is out of office "
                    "and has no available backup; assign manually"
                ),
            )
        )
        approver_id = None
        route_note = f"{base.approver_id} out of office; no available backup"
    else:
        approver_id = resolved.approver_id
        if resolved.approver_id == base.approver_id:
            route_note = f"{base.approver_id}"
        else:
            route_note = (
                f"{base.approver_id} out of office; backup {resolved.approver_id}"
            )

    if tier == "vp":
        tier_note = (
            f"effective discount {priced.effective_discount_pct:.2f}% > "
            f"{rules.thresholds.manager_max_discount_pct:.2f}% -> VP ({route_note})"
        )
    elif priced.effective_discount_pct > rules.thresholds.specialist_max_discount_pct:
        tier_note = (
            f"effective discount {priced.effective_discount_pct:.2f}% > "
            f"{rules.thresholds.specialist_max_discount_pct:.2f}% -> "
            f"regional manager {region} ({route_note})"
        )
    else:
        tier_note = (
            f"net total {_money(priced.net_total)} >= "
            f"{_money(rules.thresholds.manager_net_total_threshold_usd)} -> "
            f"regional manager {region} ({route_note})"
        )

    return RoutingDecision(
        draft_id=draft.draft_id,
        status=QuoteStatus.APPROVER_REQUIRED,
        required_role=role,
        approver_id=approver_id,
        specialist_id=specialist.approver_id if specialist else None,
        specialist_review_flags=flags,
        rationale=tier_note,
    )


def route_drafts(
    drafts: Sequence[DraftQuote],
    priced_quotes: Sequence[PricedQuote],
    reporting: ReportingData,
    rules: ApprovalRules,
    now: datetime,
    *,
    extra_flags_by_draft: Optional[Dict[str, Iterable[SpecialistFlag]]] = None,
    duplicate_of_by_draft: Optional[Dict[str, str]] = None,
) -> List[RoutingDecision]:
    """Route a batch; draft and priced lists must be in the same order."""
    extra_flags_by_draft = extra_flags_by_draft or {}
    duplicate_of_by_draft = duplicate_of_by_draft or {}
    return [
        route_quote(
            draft,
            priced,
            reporting,
            rules,
            now,
            extra_flags=extra_flags_by_draft.get(draft.draft_id, ()),
            duplicate_of=duplicate_of_by_draft.get(draft.draft_id),
        )
        for draft, priced in zip(drafts, priced_quotes)
    ]
