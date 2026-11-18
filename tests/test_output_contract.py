"""Guards the committed deliverable: outputs/routing_results.json."""
import json
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
OUTPUT = ROOT / "outputs" / "routing_results.json"

REQUIRED_KEYS = {
    "draft_id",
    "status",
    "net_total",
    "effective_discount_pct",
    "approver_id",
    "specialist_review_flags",
    "duplicate_of",
}

EXPECTED = {
    "DQ-24101": ("specialist_only", None),
    "DQ-24102": ("approver_required", "U01"),
    "DQ-24103": ("approver_required", "U05"),
    "DQ-24104": ("approver_required", "U05"),
    "DQ-24105": ("specialist_only", None),
    "DQ-24106": ("approver_required", "U04"),
    "DQ-24107": ("approver_required", "U01"),
    "DQ-24108": ("specialist_only", None),
    "DQ-24109": ("approver_required", "U04"),
    "DQ-24110": ("duplicate", None),
}


def _is_number(value):
    return isinstance(value, (int, float)) and not isinstance(value, bool)


def test_committed_output_shape_and_values():
    assert OUTPUT.is_file(), "outputs/routing_results.json is missing"
    rows = json.loads(OUTPUT.read_text(encoding="utf-8"))

    drafts = json.loads((ROOT / "draft_quotes.json").read_text(encoding="utf-8"))
    expected_order = [draft["draft_id"] for draft in drafts["drafts"]]
    assert [row["draft_id"] for row in rows] == expected_order

    for row in rows:
        assert set(row) == REQUIRED_KEYS
        assert row["status"] in {
            "specialist_only",
            "approver_required",
            "duplicate",
        }
        assert _is_number(row["net_total"])
        assert _is_number(row["effective_discount_pct"])
        assert round(row["net_total"], 2) == row["net_total"]
        assert round(row["effective_discount_pct"], 2) == (
            row["effective_discount_pct"]
        )
        assert row["approver_id"] is None or isinstance(
            row["approver_id"], str
        )
        assert row["duplicate_of"] is None or isinstance(
            row["duplicate_of"], str
        )
        assert isinstance(row["specialist_review_flags"], list)
        for flag in row["specialist_review_flags"]:
            assert set(flag) == {"line", "reason"}
            assert flag["line"] is None or isinstance(flag["line"], int)
            assert isinstance(flag["reason"], str) and flag["reason"]


def test_committed_output_routing_decisions():
    rows = json.loads(OUTPUT.read_text(encoding="utf-8"))

    for row in rows:
        expected_status, expected_approver = EXPECTED[row["draft_id"]]
        assert row["status"] == expected_status
        assert row["approver_id"] == expected_approver

    duplicate = next(row for row in rows if row["draft_id"] == "DQ-24110")
    assert duplicate["duplicate_of"] == "DQ-24102"

    # The known specialist-review cases must remain visible in the artifact.
    reasons = {
        row["draft_id"]: [flag["reason"] for flag in row["specialist_review_flags"]]
        for row in rows
    }
    assert any("RL120374 not found" in reason for reason in reasons["DQ-24103"])
    assert any("49.5h old" in reason for reason in reasons["DQ-24104"])
    assert reasons["DQ-24105"] == ["extraction confidence 0.62"]
    assert any("expired 2025-12-31" in reason for reason in reasons["DQ-24107"])
    assert any("RL110322 out of stock" in reason for reason in reasons["DQ-24108"])
    assert len(reasons["DQ-24109"]) == 2
