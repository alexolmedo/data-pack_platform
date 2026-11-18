"""SQLite persistence for quotes, approval requests and the audit log.

Approval mutations and their audit events share one transaction. A guarded
status update plus a unique approval request per draft prevents a second
approval from taking effect.
"""
import hashlib
import json
import sqlite3
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from pathlib import Path
from typing import Any, Dict, Iterator, List, Optional

from app.models import (
    ApprovalRequest,
    ApprovalRequestStatus,
    AuditEvent,
    DraftQuote,
    PricedQuote,
    QuoteStatus,
    RoutingDecision,
    SpecialistFlag,
)

SCHEMA = """
CREATE TABLE IF NOT EXISTS quotes (
    draft_id TEXT PRIMARY KEY,
    rfq_id TEXT NOT NULL,
    customer_id TEXT NOT NULL,
    customer_region TEXT NOT NULL,
    status TEXT NOT NULL,
    net_total TEXT NOT NULL,
    list_total TEXT NOT NULL,
    effective_discount_pct TEXT NOT NULL,
    proposed_discount_pct TEXT NOT NULL,
    approver_id TEXT,
    specialist_id TEXT,
    required_role TEXT,
    duplicate_of TEXT,
    specialist_review_flags TEXT NOT NULL DEFAULT '[]',
    rationale TEXT,
    lines_hash TEXT NOT NULL,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_quotes_duplicate
    ON quotes (rfq_id, lines_hash, created_at);

CREATE TABLE IF NOT EXISTS quote_lines (
    draft_id TEXT NOT NULL,
    line_no INTEGER NOT NULL,
    part_number TEXT NOT NULL,
    quantity TEXT NOT NULL,
    unit_price TEXT NOT NULL,
    list_price TEXT NOT NULL,
    price_source TEXT NOT NULL,
    stock_status TEXT,
    stock_available INTEGER,
    stock_as_of TEXT,
    PRIMARY KEY (draft_id, line_no),
    FOREIGN KEY (draft_id) REFERENCES quotes(draft_id)
);

CREATE TABLE IF NOT EXISTS approval_requests (
    request_id INTEGER PRIMARY KEY AUTOINCREMENT,
    draft_id TEXT NOT NULL UNIQUE,
    required_role TEXT NOT NULL,
    approver_id TEXT,
    specialist_id TEXT,
    status TEXT NOT NULL,
    specialist_approved_at TEXT,
    specialist_approved_by TEXT,
    created_at TEXT NOT NULL,
    decided_at TEXT,
    decided_by TEXT,
    note TEXT,
    FOREIGN KEY (draft_id) REFERENCES quotes(draft_id)
);

CREATE TABLE IF NOT EXISTS audit_events (
    event_id INTEGER PRIMARY KEY AUTOINCREMENT,
    draft_id TEXT,
    actor_id TEXT NOT NULL,
    action TEXT NOT NULL,
    status_before TEXT,
    status_after TEXT,
    before_json TEXT,
    after_json TEXT,
    created_at TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_audit_draft
    ON audit_events (draft_id, event_id);
"""

TERMINAL_STATUSES = {
    QuoteStatus.APPROVED,
    QuoteStatus.REJECTED,
    QuoteStatus.DUPLICATE,
}


class StoreError(Exception):
    """Base error for the quote store."""


class QuoteNotFound(StoreError):
    pass


class QuoteAlreadyExists(StoreError):
    pass


class QuoteAlreadyDecided(StoreError):
    pass


class NotAuthorized(StoreError):
    pass


class ApprovalOrderError(StoreError):
    pass


def _to_utc(value: datetime, fallback: Optional[datetime] = None) -> datetime:
    if value.tzinfo is None:
        tz = fallback.tzinfo if fallback is not None and fallback.tzinfo else timezone.utc
        value = value.replace(tzinfo=tz)
    return value.astimezone(timezone.utc)


def _iso(value: datetime, fallback: Optional[datetime] = None) -> str:
    return _to_utc(value, fallback).isoformat()


def _iso_or_none(
    value: Optional[datetime], fallback: Optional[datetime] = None
) -> Optional[str]:
    return _iso(value, fallback) if value is not None else None


def _parse_dt(value: Optional[str]) -> Optional[datetime]:
    return datetime.fromisoformat(value) if value else None


def _decimal(value: Any) -> Decimal:
    return Decimal(str(value))


def _quantity_key(value: Decimal) -> str:
    return format(value.normalize(), "f")


def lines_signature(draft: DraftQuote) -> str:
    """Stable hash of the lines, ignoring order and line numbers."""
    normalized = sorted(
        (
            line.part_number.strip().upper(),
            _quantity_key(line.quantity),
            (line.unit or "").strip().upper(),
        )
        for line in draft.lines
    )
    payload = json.dumps(normalized, separators=(",", ":"))
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


@dataclass(frozen=True)
class StoredQuote:
    draft_id: str
    rfq_id: str
    customer_id: str
    customer_region: str
    status: QuoteStatus
    net_total: Decimal
    list_total: Decimal
    effective_discount_pct: Decimal
    proposed_discount_pct: Decimal
    approver_id: Optional[str]
    specialist_id: Optional[str]
    required_role: Optional[str]
    duplicate_of: Optional[str]
    specialist_review_flags: List[SpecialistFlag]
    rationale: Optional[str]
    created_at: datetime
    updated_at: datetime


def _row_to_quote(row: sqlite3.Row) -> StoredQuote:
    flags = [
        SpecialistFlag.model_validate(item)
        for item in json.loads(row["specialist_review_flags"] or "[]")
    ]
    return StoredQuote(
        draft_id=row["draft_id"],
        rfq_id=row["rfq_id"],
        customer_id=row["customer_id"],
        customer_region=row["customer_region"],
        status=QuoteStatus(row["status"]),
        net_total=_decimal(row["net_total"]),
        list_total=_decimal(row["list_total"]),
        effective_discount_pct=_decimal(row["effective_discount_pct"]),
        proposed_discount_pct=_decimal(row["proposed_discount_pct"]),
        approver_id=row["approver_id"],
        specialist_id=row["specialist_id"],
        required_role=row["required_role"],
        duplicate_of=row["duplicate_of"],
        specialist_review_flags=flags,
        rationale=row["rationale"],
        created_at=datetime.fromisoformat(row["created_at"]),
        updated_at=datetime.fromisoformat(row["updated_at"]),
    )


def _row_to_request(row: sqlite3.Row) -> ApprovalRequest:
    return ApprovalRequest(
        request_id=row["request_id"],
        draft_id=row["draft_id"],
        required_role=row["required_role"],
        approver_id=row["approver_id"],
        specialist_id=row["specialist_id"],
        status=ApprovalRequestStatus(row["status"]),
        created_at=datetime.fromisoformat(row["created_at"]),
        specialist_approved_at=_parse_dt(row["specialist_approved_at"]),
        specialist_approved_by=row["specialist_approved_by"],
        decided_at=_parse_dt(row["decided_at"]),
        decided_by=row["decided_by"],
        note=row["note"],
    )


def _row_to_audit(row: sqlite3.Row) -> AuditEvent:
    return AuditEvent(
        event_id=row["event_id"],
        draft_id=row["draft_id"],
        actor_id=row["actor_id"],
        action=row["action"],
        status_before=row["status_before"],
        status_after=row["status_after"],
        before=json.loads(row["before_json"]) if row["before_json"] else None,
        after=json.loads(row["after_json"]) if row["after_json"] else None,
        created_at=datetime.fromisoformat(row["created_at"]),
    )


class QuoteStore:
    def __init__(self, path: Path, specialist_role: str = "quote_specialist") -> None:
        self._path = Path(path)
        self._path.parent.mkdir(parents=True, exist_ok=True)
        self._specialist_role = specialist_role
        self._init_schema()

    # -- connection helpers -------------------------------------------------

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(
            str(self._path), timeout=10, isolation_level=None
        )
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys = ON")
        return connection

    def _init_schema(self) -> None:
        connection = self._connect()
        try:
            connection.execute("PRAGMA journal_mode = WAL")
            connection.executescript(SCHEMA)
        finally:
            connection.close()

    @contextmanager
    def _transaction(self) -> Iterator[sqlite3.Connection]:
        connection = self._connect()
        connection.execute("BEGIN IMMEDIATE")
        try:
            yield connection
            connection.execute("COMMIT")
        except BaseException:
            connection.execute("ROLLBACK")
            raise
        finally:
            connection.close()

    # -- writes -------------------------------------------------------------

    def save_quote(
        self,
        draft: DraftQuote,
        priced: PricedQuote,
        decision: RoutingDecision,
        now: datetime,
        actor: str = "system",
    ) -> StoredQuote:
        """Persist a priced/routed quote and its audit event atomically."""
        if not (draft.draft_id == priced.draft_id == decision.draft_id):
            raise ValueError("draft, priced quote and decision must share an ID")
        if decision.status not in (
            QuoteStatus.SPECIALIST_ONLY,
            QuoteStatus.APPROVER_REQUIRED,
            QuoteStatus.DUPLICATE,
        ):
            raise ValueError(f"cannot persist a quote in status {decision.status}")

        flags_json = json.dumps(
            [flag.model_dump(mode="json") for flag in decision.specialist_review_flags],
            separators=(",", ":"),
        )
        created_at = _iso(draft.created_at, now)
        updated_at = _iso(now)
        signature = lines_signature(draft)

        with self._transaction() as connection:
            try:
                connection.execute(
                    """
                    INSERT INTO quotes (
                        draft_id, rfq_id, customer_id, customer_region, status,
                        net_total, list_total, effective_discount_pct,
                        proposed_discount_pct, approver_id, specialist_id,
                        required_role, duplicate_of, specialist_review_flags,
                        rationale, lines_hash, created_at, updated_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        draft.draft_id,
                        draft.rfq_id,
                        draft.customer_id,
                        priced.customer_region,
                        decision.status.value,
                        str(priced.net_total),
                        str(priced.list_total),
                        str(priced.effective_discount_pct),
                        str(draft.proposed_discount_pct),
                        decision.approver_id,
                        decision.specialist_id,
                        decision.required_role,
                        decision.duplicate_of,
                        flags_json,
                        decision.rationale,
                        signature,
                        created_at,
                        updated_at,
                    ),
                )
            except sqlite3.IntegrityError as exc:
                if "quotes.draft_id" in str(exc):
                    raise QuoteAlreadyExists(
                        f"{draft.draft_id} already exists"
                    ) from exc
                raise

            for line in priced.lines:
                connection.execute(
                    """
                    INSERT INTO quote_lines (
                        draft_id, line_no, part_number, quantity, unit_price,
                        list_price, price_source, stock_status,
                        stock_available, stock_as_of
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        draft.draft_id,
                        line.line,
                        line.part_number,
                        str(line.quantity),
                        str(line.unit_price),
                        str(line.list_price),
                        line.price_source.value,
                        line.stock_status.value if line.stock_status else None,
                        line.stock_available,
                        _iso_or_none(line.stock_as_of, now),
                    ),
                )

            if decision.status != QuoteStatus.DUPLICATE:
                connection.execute(
                    """
                    INSERT INTO approval_requests (
                        draft_id, required_role, approver_id, specialist_id,
                        status, created_at
                    ) VALUES (?, ?, ?, ?, ?, ?)
                    """,
                    (
                        draft.draft_id,
                        decision.required_role or self._specialist_role,
                        decision.approver_id,
                        decision.specialist_id,
                        ApprovalRequestStatus.PENDING.value,
                        updated_at,
                    ),
                )

            action = (
                "marked_duplicate"
                if decision.status == QuoteStatus.DUPLICATE
                else "routed"
            )
            after: Dict[str, Any] = {
                "status": decision.status.value,
                "net_total": str(priced.net_total),
                "effective_discount_pct": str(priced.effective_discount_pct),
                "approver_id": decision.approver_id,
                "specialist_id": decision.specialist_id,
            }
            if decision.duplicate_of:
                after["duplicate_of"] = decision.duplicate_of
            self._append_audit(
                connection,
                draft_id=draft.draft_id,
                actor_id=actor,
                action=action,
                status_before=QuoteStatus.RECEIVED.value,
                status_after=decision.status.value,
                before={"status": QuoteStatus.RECEIVED.value},
                after=after,
                at=now,
            )

        record = self.get_quote(draft.draft_id)
        assert record is not None
        return record

    def approve(
        self,
        draft_id: str,
        actor_id: str,
        actor_role: str,
        now: datetime,
        note: Optional[str] = None,
    ) -> StoredQuote:
        """Record specialist sign-off and/or the final approval.

        A specialist signs off every quote. Manager/VP approval is only
        accepted after that sign-off.
        """
        with self._transaction() as connection:
            quote = connection.execute(
                "SELECT * FROM quotes WHERE draft_id = ?", (draft_id,)
            ).fetchone()
            if quote is None:
                raise QuoteNotFound(draft_id)
            status = QuoteStatus(quote["status"])
            if status in TERMINAL_STATUSES:
                raise QuoteAlreadyDecided(
                    f"{draft_id} is already {status.value}"
                )

            request = connection.execute(
                "SELECT * FROM approval_requests WHERE draft_id = ?", (draft_id,)
            ).fetchone()
            if request is None:
                raise StoreError(f"{draft_id} has no approval request")

            is_specialist = actor_role == self._specialist_role
            is_approver = (
                request["approver_id"] is not None
                and actor_id == request["approver_id"]
            )
            if not (is_specialist or is_approver):
                raise NotAuthorized(
                    f"{actor_id} ({actor_role}) cannot approve {draft_id}"
                )

            now_iso = _iso(now)
            specialist_done = request["specialist_approved_at"] is not None
            finalized = False

            if is_specialist and not specialist_done:
                connection.execute(
                    """
                    UPDATE approval_requests
                    SET specialist_approved_at = ?, specialist_approved_by = ?
                    WHERE draft_id = ?
                    """,
                    (now_iso, actor_id, draft_id),
                )
                specialist_done = True
                self._append_audit(
                    connection,
                    draft_id=draft_id,
                    actor_id=actor_id,
                    action="specialist_approved",
                    status_before=status.value,
                    status_after=status.value,
                    before={"specialist_approved_by": None},
                    after={"specialist_approved_by": actor_id},
                    at=now,
                )
                if status == QuoteStatus.SPECIALIST_ONLY:
                    self._finalize_approval(
                        connection, draft_id, actor_id, now, status, note
                    )
                    finalized = True
            elif is_specialist and specialist_done:
                if not (is_approver and status == QuoteStatus.APPROVER_REQUIRED):
                    raise QuoteAlreadyDecided(
                        f"{draft_id} already has specialist sign-off"
                    )

            if (
                not finalized
                and is_approver
                and status == QuoteStatus.APPROVER_REQUIRED
            ):
                if not specialist_done:
                    raise ApprovalOrderError(
                        f"{draft_id} needs specialist sign-off before "
                        f"{request['required_role']} approval"
                    )
                self._finalize_approval(
                    connection, draft_id, actor_id, now, status, note
                )
                finalized = True

            if not finalized and status == QuoteStatus.SPECIALIST_ONLY:
                raise StoreError(f"could not approve {draft_id}")

        record = self.get_quote(draft_id)
        assert record is not None
        return record

    def reject(
        self,
        draft_id: str,
        actor_id: str,
        actor_role: str,
        now: datetime,
        note: Optional[str] = None,
    ) -> StoredQuote:
        with self._transaction() as connection:
            quote = connection.execute(
                "SELECT * FROM quotes WHERE draft_id = ?", (draft_id,)
            ).fetchone()
            if quote is None:
                raise QuoteNotFound(draft_id)
            status = QuoteStatus(quote["status"])
            if status in TERMINAL_STATUSES:
                raise QuoteAlreadyDecided(
                    f"{draft_id} is already {status.value}"
                )

            request = connection.execute(
                "SELECT * FROM approval_requests WHERE draft_id = ?", (draft_id,)
            ).fetchone()
            if request is None:
                raise StoreError(f"{draft_id} has no approval request")

            is_specialist = actor_role == self._specialist_role
            is_approver = (
                request["approver_id"] is not None
                and actor_id == request["approver_id"]
            )
            if not (is_specialist or is_approver):
                raise NotAuthorized(
                    f"{actor_id} ({actor_role}) cannot reject {draft_id}"
                )

            now_iso = _iso(now)
            cursor = connection.execute(
                """
                UPDATE quotes SET status = ?, updated_at = ?
                WHERE draft_id = ? AND status = ?
                """,
                (QuoteStatus.REJECTED.value, now_iso, draft_id, status.value),
            )
            if cursor.rowcount != 1:
                raise QuoteAlreadyDecided(f"{draft_id} changed concurrently")

            connection.execute(
                """
                UPDATE approval_requests
                SET status = ?, decided_at = ?, decided_by = ?,
                    note = COALESCE(?, note)
                WHERE draft_id = ?
                """,
                (
                    ApprovalRequestStatus.REJECTED.value,
                    now_iso,
                    actor_id,
                    note,
                    draft_id,
                ),
            )
            self._append_audit(
                connection,
                draft_id=draft_id,
                actor_id=actor_id,
                action="rejected",
                status_before=status.value,
                status_after=QuoteStatus.REJECTED.value,
                before={"status": status.value},
                after={"status": QuoteStatus.REJECTED.value, "decided_by": actor_id},
                at=now,
            )

        record = self.get_quote(draft_id)
        assert record is not None
        return record

    def record_edit(
        self,
        draft_id: str,
        actor_id: str,
        before: Dict[str, Any],
        after: Dict[str, Any],
        now: datetime,
        note: Optional[str] = None,
    ) -> None:
        """Audit a quote edit; repricing/rerouting happens upstream."""
        quote = self.get_quote(draft_id)
        if quote is None:
            raise QuoteNotFound(draft_id)
        payload = dict(after)
        if note:
            payload["note"] = note
        self.record_audit(
            actor_id=actor_id,
            action="edited",
            now=now,
            draft_id=draft_id,
            status_before=quote.status.value,
            status_after=quote.status.value,
            before=before,
            after=payload,
        )

    def record_audit(
        self,
        actor_id: str,
        action: str,
        now: datetime,
        *,
        draft_id: Optional[str] = None,
        status_before: Optional[str] = None,
        status_after: Optional[str] = None,
        before: Optional[Dict[str, Any]] = None,
        after: Optional[Dict[str, Any]] = None,
    ) -> None:
        with self._transaction() as connection:
            if draft_id is not None:
                exists = connection.execute(
                    "SELECT 1 FROM quotes WHERE draft_id = ?", (draft_id,)
                ).fetchone()
                if exists is None:
                    raise QuoteNotFound(draft_id)
            self._append_audit(
                connection,
                draft_id=draft_id,
                actor_id=actor_id,
                action=action,
                status_before=status_before,
                status_after=status_after,
                before=before,
                after=after,
                at=now,
            )

    def _finalize_approval(
        self,
        connection: sqlite3.Connection,
        draft_id: str,
        actor_id: str,
        now: datetime,
        status: QuoteStatus,
        note: Optional[str],
    ) -> None:
        now_iso = _iso(now)
        cursor = connection.execute(
            """
            UPDATE quotes SET status = ?, updated_at = ?
            WHERE draft_id = ? AND status = ?
            """,
            (QuoteStatus.APPROVED.value, now_iso, draft_id, status.value),
        )
        if cursor.rowcount != 1:
            raise QuoteAlreadyDecided(f"{draft_id} changed concurrently")

        connection.execute(
            """
            UPDATE approval_requests
            SET status = ?, decided_at = ?, decided_by = ?,
                note = COALESCE(?, note)
            WHERE draft_id = ?
            """,
            (
                ApprovalRequestStatus.APPROVED.value,
                now_iso,
                actor_id,
                note,
                draft_id,
            ),
        )
        self._append_audit(
            connection,
            draft_id=draft_id,
            actor_id=actor_id,
            action="approved",
            status_before=status.value,
            status_after=QuoteStatus.APPROVED.value,
            before={"status": status.value},
            after={"status": QuoteStatus.APPROVED.value, "decided_by": actor_id},
            at=now,
        )

    def _append_audit(
        self,
        connection: sqlite3.Connection,
        *,
        draft_id: Optional[str],
        actor_id: str,
        action: str,
        status_before: Optional[str],
        status_after: Optional[str],
        before: Optional[Dict[str, Any]],
        after: Optional[Dict[str, Any]],
        at: datetime,
    ) -> None:
        connection.execute(
            """
            INSERT INTO audit_events (
                draft_id, actor_id, action, status_before, status_after,
                before_json, after_json, created_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                draft_id,
                actor_id,
                action,
                status_before,
                status_after,
                json.dumps(before, separators=(",", ":"), default=str)
                if before is not None
                else None,
                json.dumps(after, separators=(",", ":"), default=str)
                if after is not None
                else None,
                _iso(at),
            ),
        )

    # -- reads --------------------------------------------------------------

    def find_duplicate(
        self, draft: DraftQuote, now: datetime, window_hours: int
    ) -> Optional[str]:
        """Return the original draft_id for the same RFQ/lines in the window."""
        cutoff = (_to_utc(now) - timedelta(hours=window_hours)).isoformat()
        signature = lines_signature(draft)
        connection = self._connect()
        try:
            row = connection.execute(
                """
                SELECT draft_id FROM quotes
                WHERE rfq_id = ?
                  AND lines_hash = ?
                  AND duplicate_of IS NULL
                  AND created_at >= ?
                  AND draft_id != ?
                ORDER BY created_at ASC, draft_id ASC
                LIMIT 1
                """,
                (draft.rfq_id, signature, cutoff, draft.draft_id),
            ).fetchone()
            return row["draft_id"] if row is not None else None
        finally:
            connection.close()

    def get_quote(self, draft_id: str) -> Optional[StoredQuote]:
        connection = self._connect()
        try:
            row = connection.execute(
                "SELECT * FROM quotes WHERE draft_id = ?", (draft_id,)
            ).fetchone()
            return _row_to_quote(row) if row is not None else None
        finally:
            connection.close()

    def list_quotes(
        self, status: Optional[QuoteStatus] = None
    ) -> List[StoredQuote]:
        connection = self._connect()
        try:
            if status is None:
                rows = connection.execute(
                    "SELECT * FROM quotes ORDER BY created_at ASC, draft_id ASC"
                ).fetchall()
            else:
                rows = connection.execute(
                    """
                    SELECT * FROM quotes WHERE status = ?
                    ORDER BY created_at ASC, draft_id ASC
                    """,
                    (status.value,),
                ).fetchall()
            return [_row_to_quote(row) for row in rows]
        finally:
            connection.close()

    def list_pending_specialist(self) -> List[StoredQuote]:
        connection = self._connect()
        try:
            rows = connection.execute(
                """
                SELECT q.* FROM quotes q
                JOIN approval_requests r ON r.draft_id = q.draft_id
                WHERE q.status IN (?, ?)
                  AND r.specialist_approved_at IS NULL
                ORDER BY q.created_at ASC, q.draft_id ASC
                """,
                (
                    QuoteStatus.SPECIALIST_ONLY.value,
                    QuoteStatus.APPROVER_REQUIRED.value,
                ),
            ).fetchall()
            return [_row_to_quote(row) for row in rows]
        finally:
            connection.close()

    def list_pending_approvals(self, approver_id: str) -> List[StoredQuote]:
        connection = self._connect()
        try:
            rows = connection.execute(
                """
                SELECT q.* FROM quotes q
                JOIN approval_requests r ON r.draft_id = q.draft_id
                WHERE q.status = ?
                  AND r.status = ?
                  AND r.approver_id = ?
                  AND r.specialist_approved_at IS NOT NULL
                ORDER BY q.created_at ASC, q.draft_id ASC
                """,
                (
                    QuoteStatus.APPROVER_REQUIRED.value,
                    ApprovalRequestStatus.PENDING.value,
                    approver_id,
                ),
            ).fetchall()
            return [_row_to_quote(row) for row in rows]
        finally:
            connection.close()

    def get_approval_request(self, draft_id: str) -> Optional[ApprovalRequest]:
        connection = self._connect()
        try:
            row = connection.execute(
                "SELECT * FROM approval_requests WHERE draft_id = ?", (draft_id,)
            ).fetchone()
            return _row_to_request(row) if row is not None else None
        finally:
            connection.close()

    def list_audit(self, draft_id: Optional[str] = None) -> List[AuditEvent]:
        connection = self._connect()
        try:
            if draft_id is None:
                rows = connection.execute(
                    "SELECT * FROM audit_events ORDER BY event_id ASC"
                ).fetchall()
            else:
                rows = connection.execute(
                    """
                    SELECT * FROM audit_events WHERE draft_id = ?
                    ORDER BY event_id ASC
                    """,
                    (draft_id,),
                ).fetchall()
            return [_row_to_audit(row) for row in rows]
        finally:
            connection.close()
