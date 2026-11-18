"""FastAPI review surface: auth, queues, approve/reject and the edit stub."""
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from app.api import create_app
from app.pipeline import process_drafts
from app.stock import StaticStockClient
from app.store import QuoteStore

ROOT = Path(__file__).resolve().parent.parent
FIXTURE = ROOT / "stock_fixture.json"


def auth(user_id):
    return {"X-User-Id": user_id}


@pytest.fixture
def api(tmp_path, settings, reporting, drafts):
    store = QuoteStore(tmp_path / "api.db")
    stock = StaticStockClient.from_fixture(FIXTURE)
    process_drafts(
        drafts,
        reporting,
        stock,
        store,
        settings.rules,
        settings.now(),
    )
    app = create_app(settings=settings, store=store, reporting=reporting)
    with TestClient(app) as client:
        yield client, store


def test_health_needs_no_auth(api):
    client, _ = api
    response = client.get("/health")
    assert response.status_code == 200
    assert response.json() == {"status": "ok"}


def test_authentication_is_required(api):
    client, _ = api
    assert client.get("/quotes").status_code == 401
    assert client.get("/quotes", headers=auth("NOPE")).status_code == 401


def test_list_quotes_and_filter_by_status(api):
    client, _ = api
    response = client.get("/quotes", headers=auth("S01"))
    assert response.status_code == 200
    payload = response.json()
    assert [row["draft_id"] for row in payload] == [
        f"DQ-2410{index}" for index in range(1, 10)
    ] + ["DQ-24110"]
    assert payload[0]["status"] == "specialist_only"

    duplicates = client.get(
        "/quotes?status=duplicate", headers=auth("S01")
    )
    assert duplicates.status_code == 200
    assert [row["draft_id"] for row in duplicates.json()] == ["DQ-24110"]


def test_quote_detail_includes_lines_request_and_audit(api):
    client, _ = api
    response = client.get("/quotes/DQ-24101", headers=auth("S01"))
    assert response.status_code == 200
    payload = response.json()

    assert payload["net_total"] == pytest.approx(435.23)
    assert len(payload["lines"]) == 3
    assert payload["approval_request"]["specialist_id"] == "S01"
    assert payload["approval_request"]["status"] == "pending"
    assert [event["action"] for event in payload["audit"]] == ["routed"]

    assert (
        client.get("/quotes/DQ-99999", headers=auth("S01")).status_code == 404
    )


def test_role_aware_queue(api):
    client, _ = api

    specialist_queue = client.get("/quotes/queue", headers=auth("S01"))
    assert len(specialist_queue.json()) == 9

    manager_queue = client.get("/quotes/queue", headers=auth("U01"))
    assert manager_queue.json() == []

    client.post("/quotes/DQ-24102/approve", headers=auth("S02"))

    manager_queue = client.get("/quotes/queue", headers=auth("U01"))
    assert [row["draft_id"] for row in manager_queue.json()] == ["DQ-24102"]


def test_specialist_approves_specialist_only_quote(api):
    client, store = api

    response = client.post(
        "/quotes/DQ-24101/approve",
        headers=auth("S01"),
        json={"note": "looks good"},
    )
    assert response.status_code == 200
    payload = response.json()
    assert payload["message"] == "quote approved"
    assert payload["quote"]["status"] == "approved"
    assert payload["approval_request"]["status"] == "approved"

    second = client.post("/quotes/DQ-24101/approve", headers=auth("S01"))
    assert second.status_code == 409


def test_only_specialist_or_routed_approver_can_decide(api):
    client, _ = api

    assert (
        client.post(
            "/quotes/DQ-24101/approve", headers=auth("U01")
        ).status_code
        == 403
    )
    assert (
        client.post(
            "/quotes/DQ-24101/reject", headers=auth("U01")
        ).status_code
        == 403
    )


def test_manager_approval_requires_specialist_first(api):
    client, store = api

    early = client.post(
        "/quotes/DQ-24102/approve", headers=auth("U01")
    )
    assert early.status_code == 409
    assert "specialist sign-off" in early.json()["detail"]

    signoff = client.post(
        "/quotes/DQ-24102/approve", headers=auth("S02")
    )
    assert signoff.status_code == 200
    body = signoff.json()
    assert "awaiting U01" in body["message"]
    assert body["quote"]["status"] == "approver_required"
    assert body["approval_request"]["specialist_approved_by"] == "S02"

    final = client.post(
        "/quotes/DQ-24102/approve",
        headers=auth("U01"),
        json={"note": "approved"},
    )
    assert final.status_code == 200
    assert final.json()["quote"]["status"] == "approved"


def test_reject_records_note_and_status(api):
    client, store = api

    response = client.post(
        "/quotes/DQ-24103/reject",
        headers=auth("U05"),
        json={"note": "margin too low"},
    )
    assert response.status_code == 200
    payload = response.json()
    assert payload["message"] == "quote rejected"
    assert payload["quote"]["status"] == "rejected"
    assert payload["approval_request"]["note"] == "margin too low"

    last = store.list_audit("DQ-24103")[-1]
    assert last.action == "rejected"
    assert last.status_after == "rejected"


def test_duplicate_cannot_be_decided(api):
    client, _ = api

    response = client.post(
        "/quotes/DQ-24110/approve", headers=auth("S02")
    )
    assert response.status_code == 409


def test_backup_approver_can_approve(api):
    client, _ = api

    signoff = client.post(
        "/quotes/DQ-24106/approve", headers=auth("S03")
    )
    assert signoff.status_code == 200
    assert "awaiting U04" in signoff.json()["message"]

    final = client.post(
        "/quotes/DQ-24106/approve", headers=auth("U04")
    )
    assert final.status_code == 200
    assert final.json()["quote"]["status"] == "approved"


def test_edit_is_audited_and_returns_501(api):
    client, store = api

    response = client.put(
        "/quotes/DQ-24101",
        headers=auth("S01"),
        json={
            "lines": [{"line": 1, "quantity": 25}],
            "note": "customer changed quantity",
        },
    )
    assert response.status_code == 501
    payload = response.json()
    assert payload["stub"] is True
    assert payload["audit_recorded"] is True
    assert payload["before"]["1"]["quantity"] == "30"
    assert payload["after"]["1"]["quantity"] == "25"
    assert payload["production_steps"]

    event = store.list_audit("DQ-24101")[-1]
    assert event.action == "edit_requested"
    assert event.after["lines"]["1"]["quantity"] == "25"
    assert event.after["note"] == "customer changed quantity"


def test_edit_rejects_unknown_lines_and_decided_quotes(api):
    client, store = api

    unknown = client.put(
        "/quotes/DQ-24101",
        headers=auth("S01"),
        json={"lines": [{"line": 99, "quantity": 1}]},
    )
    assert unknown.status_code == 400

    client.post("/quotes/DQ-24101/approve", headers=auth("S01"))
    decided = client.put(
        "/quotes/DQ-24101",
        headers=auth("S01"),
        json={"lines": [{"line": 1, "quantity": 25}]},
    )
    assert decided.status_code == 409


def test_edit_requires_authentication(api):
    client, _ = api
    response = client.put(
        "/quotes/DQ-24101",
        json={"lines": [{"line": 1, "quantity": 25}]},
    )
    assert response.status_code == 401
