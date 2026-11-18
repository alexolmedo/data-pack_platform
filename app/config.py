"""Configuration loading: approval rules from YAML, paths and URLs from env."""
import os
from datetime import datetime
from functools import lru_cache
from decimal import Decimal
from pathlib import Path
from typing import Mapping, Optional
from zoneinfo import ZoneInfo

import yaml
from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

ROOT = Path(__file__).resolve().parent.parent

DEFAULT_CONFIG_PATH = "config/approval_rules.yml"
DEFAULT_REPORTING_DB_PATH = "reporting_db"
DEFAULT_DATABASE_PATH = "data/quotes.db"
DEFAULT_OUTPUT_PATH = "outputs/routing_results.json"
DEFAULT_STOCK_API_URL = "http://localhost:8080"
DEFAULT_TIMEZONE = "America/Chicago"


class Thresholds(BaseModel):
    specialist_max_discount_pct: Decimal
    manager_max_discount_pct: Decimal
    manager_net_total_threshold_usd: Decimal
    low_confidence_threshold: Decimal
    stock_stale_hours: int = Field(gt=0)
    reporting_stale_hours: int = Field(gt=0)
    duplicate_window_hours: int = Field(gt=0)

    @model_validator(mode="after")
    def _check_ranges(self) -> "Thresholds":
        if not (Decimal("0") <= self.specialist_max_discount_pct < self.manager_max_discount_pct <= Decimal("100")):
            raise ValueError(
                "expected 0 <= specialist_max_discount_pct "
                "< manager_max_discount_pct <= 100"
            )
        if self.manager_net_total_threshold_usd <= 0:
            raise ValueError("manager_net_total_threshold_usd must be positive")
        if not (Decimal("0") <= self.low_confidence_threshold <= Decimal("1")):
            raise ValueError("low_confidence_threshold must be between 0 and 1")
        return self


class RetryPolicy(BaseModel):
    max_attempts: int = Field(default=4, ge=1, le=10)
    timeout_seconds: float = Field(default=2.0, gt=0)
    initial_backoff_seconds: float = Field(default=0.5, ge=0)
    max_backoff_seconds: float = Field(default=4.0, ge=0)
    batch_size: int = Field(default=50, ge=1, le=50)
    jitter: bool = True


class RoleNames(BaseModel):
    specialist: str = "quote_specialist"
    regional_manager: str = "regional_manager"
    vp: str = "vp_sales"


class SpecialistPolicy(BaseModel):
    # prepared_by | first_available
    assignment: str = "prepared_by"

    @field_validator("assignment")
    @classmethod
    def _check_assignment(cls, value: str) -> str:
        if value not in ("prepared_by", "first_available"):
            raise ValueError("specialist.assignment must be 'prepared_by' or 'first_available'")
        return value


class ApprovalRules(BaseModel):
    version: int = 1
    timezone: str = DEFAULT_TIMEZONE
    policy_now: Optional[datetime] = None
    thresholds: Thresholds
    retry: RetryPolicy = Field(default_factory=RetryPolicy)
    roles: RoleNames = Field(default_factory=RoleNames)
    specialist: SpecialistPolicy = Field(default_factory=SpecialistPolicy)

    @field_validator("timezone")
    @classmethod
    def _check_timezone(cls, value: str) -> str:
        ZoneInfo(value)  # raises ZoneInfoNotFoundError for an invalid name
        return value

    @field_validator("policy_now")
    @classmethod
    def _attach_timezone(cls, value: Optional[datetime], info) -> Optional[datetime]:
        if value is not None and value.tzinfo is None:
            tz_name = info.data.get("timezone", DEFAULT_TIMEZONE)
            return value.replace(tzinfo=ZoneInfo(tz_name))
        return value

    @property
    def zone(self) -> ZoneInfo:
        return ZoneInfo(self.timezone)


class Settings(BaseModel):
    model_config = ConfigDict(arbitrary_types_allowed=True)

    root: Path
    config_path: Path
    reporting_db_path: Path
    database_path: Path
    output_path: Path
    stock_api_url: str
    rules: ApprovalRules

    def now(self) -> datetime:
        """Policy clock: the sample-run override when configured, else real time."""
        return self.rules.policy_now or datetime.now(self.rules.zone)


def _resolve(root: Path, value: str) -> Path:
    path = Path(value).expanduser()
    return path if path.is_absolute() else (root / path).resolve()


def load_rules(path: Path) -> ApprovalRules:
    with path.open("r", encoding="utf-8") as handle:
        data = yaml.safe_load(handle) or {}
    return ApprovalRules.model_validate(data)


def _parse_policy_now(value: str, zone: ZoneInfo) -> datetime:
    # Python 3.9's fromisoformat does not accept a trailing "Z".
    parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    return parsed if parsed.tzinfo is not None else parsed.replace(tzinfo=zone)


def load_settings(
    env: Optional[Mapping[str, str]] = None,
    root: Optional[Path] = None,
) -> Settings:
    """Build settings from env overrides without mutating global state.

    Passing ``env={}`` is useful in tests: it ignores the developer's shell
    environment and loads the checked-in defaults.
    """
    values = dict(os.environ if env is None else env)
    base = Path(root).resolve() if root is not None else ROOT

    config_path = _resolve(base, values.get("RIDGELINE_CONFIG", DEFAULT_CONFIG_PATH))
    rules = load_rules(config_path)

    policy_override = values.get("POLICY_NOW")
    if policy_override:
        rules = rules.model_copy(
            update={"policy_now": _parse_policy_now(policy_override, rules.zone)}
        )

    return Settings(
        root=base,
        config_path=config_path,
        reporting_db_path=_resolve(
            base, values.get("REPORTING_DB_PATH", DEFAULT_REPORTING_DB_PATH)
        ),
        database_path=_resolve(
            base, values.get("APP_DB_PATH", DEFAULT_DATABASE_PATH)
        ),
        output_path=_resolve(
            base, values.get("ROUTING_OUTPUT_PATH", DEFAULT_OUTPUT_PATH)
        ),
        stock_api_url=values.get("STOCK_API_URL", DEFAULT_STOCK_API_URL).rstrip("/"),
        rules=rules,
    )


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    """Process-wide settings; call get_settings.cache_clear() in tests."""
    return load_settings()
