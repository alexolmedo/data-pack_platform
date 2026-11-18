"""Read-only loaders for the nightly reporting database export.

The export is a snapshot copied from SAP; this service never writes to it or
to SAP. Everything is loaded once and then queried from memory.
"""
import csv
import json
from dataclasses import dataclass
from datetime import date, datetime
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Dict, Iterable, List, Optional, Tuple

from app.models import Approver, ContractPrice, Customer, Product

REQUIRED_FILES = (
    "products.csv",
    "customers.csv",
    "contract_prices.csv",
    "approvers.csv",
)
METADATA_FILE = "export_metadata.json"


class ReportingDataError(Exception):
    """The export is missing, malformed, or internally inconsistent."""


def _clean(value: Optional[str]) -> Optional[str]:
    if value is None:
        return None
    stripped = value.strip()
    return stripped or None


def _require(value: Optional[str], field: str, path: Path, row: int) -> str:
    cleaned = _clean(value)
    if cleaned is None:
        raise ReportingDataError(f"{path.name} row {row}: missing {field}")
    return cleaned


def _to_decimal(value: Optional[str], field: str, path: Path, row: int) -> Decimal:
    raw = _require(value, field, path, row)
    try:
        return Decimal(raw)
    except InvalidOperation as exc:
        raise ReportingDataError(
            f"{path.name} row {row}: invalid {field} {raw!r}"
        ) from exc


def _to_date(value: Optional[str], field: str, path: Path, row: int) -> date:
    raw = _require(value, field, path, row)
    try:
        return date.fromisoformat(raw)
    except ValueError as exc:
        raise ReportingDataError(
            f"{path.name} row {row}: invalid {field} {raw!r}"
        ) from exc


def _to_datetime(value: Optional[str], field: str, path: Path) -> Optional[datetime]:
    raw = _clean(value)
    if raw is None:
        return None
    try:
        return datetime.fromisoformat(raw.replace("Z", "+00:00"))
    except ValueError as exc:
        raise ReportingDataError(
            f"{path.name}: invalid {field} {raw!r}"
        ) from exc


def _rows(path: Path) -> Iterable[Tuple[int, Dict[str, str]]]:
    with path.open("r", encoding="utf-8-sig", newline="") as handle:
        reader = csv.DictReader(handle)
        if reader.fieldnames is None:
            raise ReportingDataError(f"{path.name}: missing header row")
        for row_number, row in enumerate(reader, start=2):
            yield row_number, row


@dataclass(frozen=True)
class ContractBook:
    """Contract prices indexed by (customer, part), newest first."""

    entries: Dict[Tuple[str, str], List[ContractPrice]]

    @classmethod
    def from_prices(cls, prices: Iterable[ContractPrice]) -> "ContractBook":
        index: Dict[Tuple[str, str], List[ContractPrice]] = {}
        for price in prices:
            index.setdefault((price.customer_id, price.part_number), []).append(price)
        for candidates in index.values():
            # Prefer the most recently effective contract when several exist.
            candidates.sort(key=lambda item: (item.valid_from, item.valid_to), reverse=True)
        return cls(entries=index)

    def candidates(self, customer_id: str, part_number: str) -> List[ContractPrice]:
        return list(self.entries.get((customer_id, part_number), []))

    def valid_on(
        self, customer_id: str, part_number: str, day: date
    ) -> Optional[ContractPrice]:
        for candidate in self.candidates(customer_id, part_number):
            if candidate.is_valid_on(day):
                return candidate
        return None

    def most_recent(self, customer_id: str, part_number: str) -> Optional[ContractPrice]:
        candidates = self.candidates(customer_id, part_number)
        return candidates[0] if candidates else None


@dataclass(frozen=True)
class ReportingData:
    source_dir: Path
    products: Dict[str, Product]
    customers: Dict[str, Customer]
    contracts: ContractBook
    approvers: Dict[str, Approver]
    exported_at: Optional[datetime]

    def product(self, part_number: str) -> Optional[Product]:
        return self.products.get(part_number)

    def customer(self, customer_id: str) -> Optional[Customer]:
        return self.customers.get(customer_id)

    def approver(self, approver_id: str) -> Optional[Approver]:
        return self.approvers.get(approver_id)

    def approvers_with_role(self, role: str) -> List[Approver]:
        return [a for a in self.approvers.values() if a.role == role]


def load_products(path: Path) -> Dict[str, Product]:
    products: Dict[str, Product] = {}
    for row_number, row in _rows(path):
        part_number = _require(row.get("part_number"), "part_number", path, row_number)
        products[part_number] = Product(
            part_number=part_number,
            description=_clean(row.get("description")),
            category=_clean(row.get("category")),
            unit=_clean(row.get("unit")),
            list_price=_to_decimal(row.get("list_price"), "list_price", path, row_number),
            status=_clean(row.get("status")),
            replaced_by=_clean(row.get("replaced_by")),
            primary_warehouse=_clean(row.get("primary_warehouse")),
        )
    if not products:
        raise ReportingDataError(f"{path.name}: no product rows")
    return products


def load_customers(path: Path) -> Dict[str, Customer]:
    customers: Dict[str, Customer] = {}
    for row_number, row in _rows(path):
        customer_id = _require(row.get("customer_id"), "customer_id", path, row_number)
        customers[customer_id] = Customer(
            customer_id=customer_id,
            name=_require(row.get("name"), "name", path, row_number),
            region=_require(row.get("region"), "region", path, row_number),
            pricing_tier=_clean(row.get("pricing_tier")),
        )
    if not customers:
        raise ReportingDataError(f"{path.name}: no customer rows")
    return customers


def load_contract_prices(path: Path) -> ContractBook:
    prices: List[ContractPrice] = []
    for row_number, row in _rows(path):
        prices.append(
            ContractPrice(
                customer_id=_require(
                    row.get("customer_id"), "customer_id", path, row_number
                ),
                part_number=_require(
                    row.get("part_number"), "part_number", path, row_number
                ),
                contract_price=_to_decimal(
                    row.get("contract_price"), "contract_price", path, row_number
                ),
                valid_from=_to_date(
                    row.get("valid_from"), "valid_from", path, row_number
                ),
                valid_to=_to_date(row.get("valid_to"), "valid_to", path, row_number),
            )
        )
    return ContractBook.from_prices(prices)


def load_approvers(path: Path) -> Dict[str, Approver]:
    approvers: Dict[str, Approver] = {}
    for row_number, row in _rows(path):
        approver_id = _require(row.get("approver_id"), "approver_id", path, row_number)
        start = _clean(row.get("out_of_office_from"))
        end = _clean(row.get("out_of_office_to"))
        approvers[approver_id] = Approver(
            approver_id=approver_id,
            name=_require(row.get("name"), "name", path, row_number),
            role=_require(row.get("role"), "role", path, row_number),
            region=_clean(row.get("region")),
            backup_approver_id=_clean(row.get("backup_approver_id")),
            out_of_office_from=date.fromisoformat(start) if start else None,
            out_of_office_to=date.fromisoformat(end) if end else None,
        )
    if not approvers:
        raise ReportingDataError(f"{path.name}: no approver rows")
    return approvers


def load_exported_at(path: Path) -> Optional[datetime]:
    if not path.exists():
        return None
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise ReportingDataError(f"{path.name}: cannot read export metadata") from exc
    return _to_datetime(payload.get("exported_at"), "exported_at", path)


def load_reporting_data(directory: Path) -> ReportingData:
    """Load every table and the export timestamp from a reporting snapshot."""
    source = Path(directory)
    if not source.is_dir():
        raise ReportingDataError(f"reporting directory not found: {source}")

    missing = [name for name in REQUIRED_FILES if not (source / name).is_file()]
    if missing:
        raise ReportingDataError(
            f"reporting export missing required file(s): {', '.join(missing)}"
        )

    return ReportingData(
        source_dir=source,
        products=load_products(source / "products.csv"),
        customers=load_customers(source / "customers.csv"),
        contracts=load_contract_prices(source / "contract_prices.csv"),
        approvers=load_approvers(source / "approvers.csv"),
        exported_at=load_exported_at(source / METADATA_FILE),
    )


def reporting_age_hours(exported_at: Optional[datetime], now: datetime) -> Optional[float]:
    if exported_at is None:
        return None
    reference = exported_at
    if reference.tzinfo is None and now.tzinfo is not None:
        reference = reference.replace(tzinfo=now.tzinfo)
    return (now - reference).total_seconds() / 3600.0


def is_reporting_stale(
    exported_at: Optional[datetime], now: datetime, max_age_hours: int
) -> bool:
    """Missing or older than the limit both count as 'cannot confirm'."""
    age = reporting_age_hours(exported_at, now)
    return age is None or age > max_age_hours
