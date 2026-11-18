"""Domain models shared by pricing, routing, storage and the API.

Python 3.9 compatible: use typing.Optional/List/Dict rather than PEP 604.
"""
from datetime import date, datetime
from decimal import Decimal
from enum import Enum
from typing import Any, Dict, List, Optional

from pydantic import BaseModel, ConfigDict, Field, field_serializer


class QuoteStatus(str, Enum):
    """Internal lifecycle and the three statuses required in the output."""

    RECEIVED = "received"
    PRICED = "priced"
    SPECIALIST_REVIEW = "specialist_review"
    SPECIALIST_ONLY = "specialist_only"
    APPROVER_REQUIRED = "approver_required"
    DUPLICATE = "duplicate"
    APPROVED = "approved"
    REJECTED = "rejected"


class ApproverRole(str, Enum):
    QUOTE_SPECIALIST = "quote_specialist"
    REGIONAL_MANAGER = "regional_manager"
    SALES_OPERATIONS_DIRECTOR = "sales_operations_director"
    VP_SALES = "vp_sales"


class PriceSource(str, Enum):
    CONTRACT = "contract"
    LIST = "list"
    EXPIRED_CONTRACT = "expired_contract"


class StockStatus(str, Enum):
    OK = "ok"
    NOT_FOUND = "not_found"
    OUT_OF_STOCK = "out_of_stock"
    UNAVAILABLE = "unavailable"


class ApprovalRequestStatus(str, Enum):
    PENDING = "pending"
    APPROVED = "approved"
    REJECTED = "rejected"
    CANCELLED = "cancelled"


class DraftLine(BaseModel):
    line: int
    part_number: str
    description: Optional[str] = None
    quantity: Decimal
    unit: Optional[str] = None
    extraction_confidence: Optional[Decimal] = None


class DraftQuote(BaseModel):
    draft_id: str
    rfq_id: str
    customer_id: str
    created_at: datetime
    prepared_by: Optional[str] = None
    proposed_discount_pct: Decimal
    lines: List[DraftLine]
    note: Optional[str] = None


class Customer(BaseModel):
    customer_id: str
    name: str
    region: str
    pricing_tier: Optional[str] = None


class Product(BaseModel):
    part_number: str
    description: Optional[str] = None
    category: Optional[str] = None
    unit: Optional[str] = None
    list_price: Decimal
    status: Optional[str] = None
    replaced_by: Optional[str] = None
    primary_warehouse: Optional[str] = None


class ContractPrice(BaseModel):
    customer_id: str
    part_number: str
    contract_price: Decimal
    valid_from: date
    valid_to: date

    def is_valid_on(self, day: date) -> bool:
        return self.valid_from <= day <= self.valid_to


class Approver(BaseModel):
    approver_id: str
    name: str
    role: str
    region: Optional[str] = None
    backup_approver_id: Optional[str] = None
    out_of_office_from: Optional[date] = None
    out_of_office_to: Optional[date] = None

    def is_out_of_office(self, day: date) -> bool:
        """Inclusive date-range check; an open start means currently out."""
        if self.out_of_office_from is None:
            return False
        if self.out_of_office_to is None:
            return day >= self.out_of_office_from
        return self.out_of_office_from <= day <= self.out_of_office_to

    def covers_region(self, region: str) -> bool:
        return self.region in (region, "ALL")


class StockSnapshot(BaseModel):
    part_number: str
    warehouses: Dict[str, int]
    as_of: datetime

    @property
    def total_quantity(self) -> int:
        return sum(self.warehouses.values())


class SpecialistFlag(BaseModel):
    """A reason the specialist must look at this quote.

    ``line`` is the 1-based line number, or ``None`` for a whole-quote issue.
    """

    line: Optional[int] = None
    reason: str


class PricedLine(BaseModel):
    line: int
    part_number: str
    quantity: Decimal
    unit_price: Decimal
    list_price: Decimal
    price_source: PriceSource
    stock_status: Optional[StockStatus] = None
    stock_as_of: Optional[datetime] = None
    stock_available: Optional[int] = None
    flags: List[SpecialistFlag] = Field(default_factory=list)


class PricedQuote(BaseModel):
    draft_id: str
    rfq_id: str
    customer_id: str
    customer_region: str
    proposed_discount_pct: Decimal
    lines: List[PricedLine]
    net_total: Decimal
    list_total: Decimal
    effective_discount_pct: Decimal
    reporting_exported_at: Optional[datetime] = None
    specialist_review_flags: List[SpecialistFlag] = Field(default_factory=list)
    computed_at: Optional[datetime] = None


class RoutingDecision(BaseModel):
    draft_id: str
    status: QuoteStatus
    required_role: Optional[str] = None
    approver_id: Optional[str] = None
    specialist_id: Optional[str] = None
    specialist_review_flags: List[SpecialistFlag] = Field(default_factory=list)
    duplicate_of: Optional[str] = None
    rationale: Optional[str] = None


class RoutingResult(BaseModel):
    """One row of outputs/routing_results.json (the required field set)."""

    model_config = ConfigDict(extra="forbid")

    draft_id: str
    status: QuoteStatus
    net_total: Decimal
    effective_discount_pct: Decimal
    approver_id: Optional[str] = None
    specialist_review_flags: List[SpecialistFlag] = Field(default_factory=list)
    duplicate_of: Optional[str] = None

    @field_serializer("net_total", "effective_discount_pct")
    def _serialize_money(self, value: Decimal) -> float:
        return float(value.quantize(Decimal("0.01")))


class ApprovalRequest(BaseModel):
    request_id: Optional[int] = None
    draft_id: str
    required_role: str
    approver_id: Optional[str] = None
    specialist_id: Optional[str] = None
    status: ApprovalRequestStatus = ApprovalRequestStatus.PENDING
    created_at: datetime
    specialist_approved_at: Optional[datetime] = None
    specialist_approved_by: Optional[str] = None
    decided_at: Optional[datetime] = None
    decided_by: Optional[str] = None
    note: Optional[str] = None


class AuditEvent(BaseModel):
    """Append-only record of who did what, when, and the status change."""

    event_id: Optional[int] = None
    draft_id: Optional[str] = None
    actor_id: str
    action: str
    status_before: Optional[str] = None
    status_after: Optional[str] = None
    before: Optional[Dict[str, Any]] = None
    after: Optional[Dict[str, Any]] = None
    created_at: datetime
