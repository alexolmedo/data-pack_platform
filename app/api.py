"""FastAPI review surface for specialists and routed approvers.

Authentication is deliberately simple: ``X-User-Id`` identifies a person in
the reporting export, and their role comes from ``approvers.csv``. Production
would use the corporate IdP and signed tokens.
"""
from datetime import datetime
from decimal import Decimal
from typing import Dict, List, Optional

from fastapi import Depends, FastAPI, Header, HTTPException, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field

from app.config import Settings, get_settings
from app.models import (
    ApprovalRequest,
    Approver,
    AuditEvent,
    QuoteStatus,
    SpecialistFlag,
)
from app.reporting import ReportingData, load_reporting_data
from app.store import (
    ApprovalOrderError,
    NotAuthorized,
    QuoteAlreadyDecided,
    QuoteAlreadyExists,
    QuoteNotFound,
    QuoteStore,
    StoreError,
    StoredLine,
    StoredQuote,
)


class QuoteSummary(BaseModel):
    draft_id: str
    rfq_id: str
    customer_id: str
    customer_region: str
    status: QuoteStatus
    net_total: float
    list_total: float
    effective_discount_pct: float
    proposed_discount_pct: float
    approver_id: Optional[str] = None
    specialist_id: Optional[str] = None
    required_role: Optional[str] = None
    duplicate_of: Optional[str] = None
    specialist_review_flags: List[SpecialistFlag] = Field(default_factory=list)
    rationale: Optional[str] = None
    created_at: datetime
    updated_at: datetime


class QuoteLineResponse(BaseModel):
    line: int
    part_number: str
    quantity: float
    unit_price: float
    list_price: float
    price_source: str
    stock_status: Optional[str] = None
    stock_available: Optional[int] = None
    stock_as_of: Optional[datetime] = None


class QuoteDetailResponse(QuoteSummary):
    lines: List[QuoteLineResponse] = Field(default_factory=list)
    approval_request: Optional[ApprovalRequest] = None
    audit: List[AuditEvent] = Field(default_factory=list)


class ApprovalAction(BaseModel):
    note: Optional[str] = None


class ApprovalResponse(BaseModel):
    message: str
    quote: QuoteSummary
    approval_request: Optional[ApprovalRequest] = None


class LineEdit(BaseModel):
    line: int
    part_number: Optional[str] = None
    quantity: Optional[Decimal] = None


class EditQuoteRequest(BaseModel):
    lines: List[LineEdit] = Field(default_factory=list)
    note: Optional[str] = None


def _summary(record: StoredQuote) -> QuoteSummary:
    return QuoteSummary(
        draft_id=record.draft_id,
        rfq_id=record.rfq_id,
        customer_id=record.customer_id,
        customer_region=record.customer_region,
        status=record.status,
        net_total=float(record.net_total),
        list_total=float(record.list_total),
        effective_discount_pct=float(record.effective_discount_pct),
        proposed_discount_pct=float(record.proposed_discount_pct),
        approver_id=record.approver_id,
        specialist_id=record.specialist_id,
        required_role=record.required_role,
        duplicate_of=record.duplicate_of,
        specialist_review_flags=record.specialist_review_flags,
        rationale=record.rationale,
        created_at=record.created_at,
        updated_at=record.updated_at,
    )


def _line_response(line: StoredLine) -> QuoteLineResponse:
    return QuoteLineResponse(
        line=line.line,
        part_number=line.part_number,
        quantity=float(line.quantity),
        unit_price=float(line.unit_price),
        list_price=float(line.list_price),
        price_source=line.price_source,
        stock_status=line.stock_status,
        stock_available=line.stock_available,
        stock_as_of=line.stock_as_of,
    )


def _detail(store: QuoteStore, record: StoredQuote) -> QuoteDetailResponse:
    return QuoteDetailResponse(
        **_summary(record).model_dump(),
        lines=[_line_response(line) for line in store.get_quote_lines(record.draft_id)],
        approval_request=store.get_approval_request(record.draft_id),
        audit=store.list_audit(record.draft_id),
    )


def _approval_response(store: QuoteStore, record: StoredQuote) -> ApprovalResponse:
    request = store.get_approval_request(record.draft_id)
    if record.status is QuoteStatus.APPROVED:
        message = "quote approved"
    elif record.status is QuoteStatus.REJECTED:
        message = "quote rejected"
    elif request is not None and request.specialist_approved_at is not None:
        assignee = request.approver_id or request.required_role
        message = f"specialist sign-off recorded; awaiting {assignee}"
    else:
        message = "specialist sign-off recorded"
    return ApprovalResponse(
        message=message,
        quote=_summary(record),
        approval_request=request,
    )


def create_app(
    settings: Optional[Settings] = None,
    store: Optional[QuoteStore] = None,
    reporting: Optional[ReportingData] = None,
) -> FastAPI:
    settings = settings or get_settings()
    reporting = reporting or load_reporting_data(settings.reporting_db_path)
    store = store or QuoteStore(
        settings.database_path,
        specialist_role=settings.rules.roles.specialist,
    )
    specialist_role = settings.rules.roles.specialist

    application = FastAPI(
        title="Ridgeline Quote Approvals",
        version="1.0.0",
        description="Pricing, stock and approval routing for draft quotes.",
    )

    def actor_dependency(
        x_user_id: Optional[str] = Header(default=None, alias="X-User-Id")
    ) -> Approver:
        if not x_user_id:
            raise HTTPException(
                status_code=401, detail="X-User-Id header is required"
            )
        actor = reporting.approver(x_user_id)
        if actor is None:
            raise HTTPException(
                status_code=401, detail=f"unknown user {x_user_id}"
            )
        return actor

    @application.exception_handler(QuoteNotFound)
    async def _quote_not_found(request: Request, exc: QuoteNotFound):
        return JSONResponse(status_code=404, content={"detail": str(exc)})

    @application.exception_handler(NotAuthorized)
    async def _not_authorized(request: Request, exc: NotAuthorized):
        return JSONResponse(status_code=403, content={"detail": str(exc)})

    @application.exception_handler(QuoteAlreadyDecided)
    async def _already_decided(request: Request, exc: QuoteAlreadyDecided):
        return JSONResponse(status_code=409, content={"detail": str(exc)})

    @application.exception_handler(ApprovalOrderError)
    async def _approval_order(request: Request, exc: ApprovalOrderError):
        return JSONResponse(status_code=409, content={"detail": str(exc)})

    @application.exception_handler(QuoteAlreadyExists)
    async def _already_exists(request: Request, exc: QuoteAlreadyExists):
        return JSONResponse(status_code=409, content={"detail": str(exc)})

    @application.exception_handler(StoreError)
    async def _store_error(request: Request, exc: StoreError):
        return JSONResponse(status_code=500, content={"detail": str(exc)})

    @application.get("/health")
    def health() -> Dict[str, str]:
        return {"status": "ok"}

    @application.get("/quotes", response_model=List[QuoteSummary])
    def list_quotes(
        status: Optional[QuoteStatus] = None,
        actor: Approver = Depends(actor_dependency),
    ) -> List[QuoteSummary]:
        return [_summary(record) for record in store.list_quotes(status)]

    @application.get("/quotes/queue", response_model=List[QuoteSummary])
    def my_queue(
        actor: Approver = Depends(actor_dependency),
    ) -> List[QuoteSummary]:
        if actor.role == specialist_role:
            records = store.list_pending_specialist()
        else:
            records = store.list_pending_approvals(actor.approver_id)
        return [_summary(record) for record in records]

    @application.get(
        "/quotes/{draft_id}", response_model=QuoteDetailResponse
    )
    def get_quote(
        draft_id: str,
        actor: Approver = Depends(actor_dependency),
    ) -> QuoteDetailResponse:
        record = store.get_quote(draft_id)
        if record is None:
            raise QuoteNotFound(draft_id)
        return _detail(store, record)

    @application.post(
        "/quotes/{draft_id}/approve", response_model=ApprovalResponse
    )
    def approve_quote(
        draft_id: str,
        action: Optional[ApprovalAction] = None,
        actor: Approver = Depends(actor_dependency),
    ) -> ApprovalResponse:
        note = action.note if action is not None else None
        record = store.approve(
            draft_id, actor.approver_id, actor.role, settings.now(), note
        )
        return _approval_response(store, record)

    @application.post(
        "/quotes/{draft_id}/reject", response_model=ApprovalResponse
    )
    def reject_quote(
        draft_id: str,
        action: Optional[ApprovalAction] = None,
        actor: Approver = Depends(actor_dependency),
    ) -> ApprovalResponse:
        note = action.note if action is not None else None
        record = store.reject(
            draft_id, actor.approver_id, actor.role, settings.now(), note
        )
        return _approval_response(store, record)

    @application.put("/quotes/{draft_id}")
    def edit_quote(
        draft_id: str,
        edit: EditQuoteRequest,
        actor: Approver = Depends(actor_dependency),
    ):
        """Stubbed edit: audit the request, then return 501.

        Production would apply the change, call ``price_quote`` again, re-run
        the stock check and ``route_quote``, cancel the old approval request,
        and audit the before/after of every changed field.
        """
        record = store.get_quote(draft_id)
        if record is None:
            raise QuoteNotFound(draft_id)
        if record.status in (
            QuoteStatus.APPROVED,
            QuoteStatus.REJECTED,
            QuoteStatus.DUPLICATE,
        ):
            raise HTTPException(
                status_code=409,
                detail=f"{draft_id} is {record.status.value}; edits are not allowed",
            )
        if not edit.lines:
            raise HTTPException(
                status_code=400, detail="at least one line edit is required"
            )

        existing = store.get_quote_lines(draft_id)
        by_line = {line.line: line for line in existing}
        for change in edit.lines:
            if change.line not in by_line:
                raise HTTPException(
                    status_code=400,
                    detail=f"line {change.line} does not exist on {draft_id}",
                )

        before: Dict[str, Dict[str, object]] = {
            str(line.line): {
                "part_number": line.part_number,
                "quantity": str(line.quantity),
                "unit_price": str(line.unit_price),
                "list_price": str(line.list_price),
            }
            for line in existing
        }
        after = {key: dict(value) for key, value in before.items()}
        for change in edit.lines:
            key = str(change.line)
            if change.part_number is not None:
                after[key]["part_number"] = change.part_number
            if change.quantity is not None:
                after[key]["quantity"] = str(change.quantity)

        store.record_audit(
            actor_id=actor.approver_id,
            action="edit_requested",
            now=settings.now(),
            draft_id=draft_id,
            status_before=record.status.value,
            status_after=record.status.value,
            before={"lines": before},
            after={"lines": after, "note": edit.note},
        )

        return JSONResponse(
            status_code=501,
            content={
                "detail": "quote editing is stubbed in this build",
                "stub": True,
                "audit_recorded": True,
                "before": before,
                "after": after,
                "production_steps": [
                    "apply the line changes and re-run price_quote",
                    "repeat the stock check and refresh specialist flags",
                    "re-run route_quote and apply OOO backup rules",
                    "cancel the previous approval request and notify the new approver",
                ],
            },
        )

    return application
