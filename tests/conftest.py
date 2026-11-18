"""Shared fixtures: real sample data, deterministic policy clock."""
import json
from pathlib import Path
from typing import Dict

import pytest

from app.config import Settings, load_settings
from app.models import DraftQuote
from app.reporting import ReportingData, load_reporting_data

ROOT = Path(__file__).resolve().parent.parent


@pytest.fixture(scope="session")
def settings() -> Settings:
    return load_settings(env={}, root=ROOT)


@pytest.fixture(scope="session")
def reporting(settings: Settings) -> ReportingData:
    return load_reporting_data(settings.reporting_db_path)


@pytest.fixture(scope="session")
def drafts_by_id() -> Dict[str, DraftQuote]:
    payload = json.loads((ROOT / "draft_quotes.json").read_text(encoding="utf-8"))
    drafts = [DraftQuote.model_validate(item) for item in payload["drafts"]]
    return {draft.draft_id: draft for draft in drafts}
