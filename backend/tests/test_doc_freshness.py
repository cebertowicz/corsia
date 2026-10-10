from __future__ import annotations

from collections import Counter
from datetime import datetime, timedelta, timezone
from pathlib import Path
import re

from registry.doc_freshness import CADENCE_DAYS, format_freshness, freshness

_NOW = datetime(2026, 9, 30, 12, tzinfo=timezone.utc)
_VERIFIED = (_NOW - timedelta(days=10)).isoformat()


def test_freshness_class_table_and_non_expiring_classes():
    cases = {
        "/scope/shared/archive/old.md": ("archived", "archived", None),
        "/scope/shared/records/2026-09-12-audit.md": (
            "dated_evidence", "as_of", None,
        ),
        "/scope/shared/planning/DRAFT-next.md": ("draft", "draft_expiring", 30),
        "/scope/shared/standards/naming.md": ("standard", "current", 90),
        "/scope/shared/architecture/TEAM-CHARTER.md": ("standard", "current", 90),
        "/scope/shared/planning/PRD-next.md": ("prd_rfc", "current", 30),
        "/scope/shared/planning/RFC-next.md": ("prd_rfc", "current", 30),
        "/scope/shared/documents/reference.md": (
            "domain_reference", "current", 30,
        ),
    }

    for path, (expected_class, state, cadence) in cases.items():
        result = freshness(path, {"last_verified_at": _VERIFIED}, _NOW)
        assert result["class"] == expected_class
        assert result["state"] == state
        if cadence is None:
            assert result["due_at"] is None
        else:
            verified = datetime.fromisoformat(result["verified_at"])
            assert datetime.fromisoformat(result["due_at"]) == verified + timedelta(
                days=cadence
            )

    dated = freshness(
        "/scope/shared/records/2026-09-12-audit.md", {}, _NOW
    )
    assert dated["verified_at"] == "2026-09-12"
    assert dated["age_days"] == 18


def test_boundary_is_overdue_exactly_at_due_at_and_review_text_cannot_extend_it():
    verified = _NOW - timedelta(days=30)
    metadata = {
        "last_verified_at": verified.isoformat(),
        "Review": "after 999 days",
        "update_trigger": "review after 999 days",
    }

    at_boundary = freshness(
        "/scope/shared/documents/reference.md", metadata, _NOW
    )
    just_before = freshness(
        "/scope/shared/documents/reference.md",
        metadata,
        _NOW - timedelta(microseconds=1),
    )

    assert at_boundary["state"] == "overdue"
    assert just_before["state"] == "current"
    assert datetime.fromisoformat(at_boundary["due_at"]) == verified + timedelta(
        days=30
    )


def test_never_verified_is_distinct_from_overdue_and_formats_separately():
    rows = [
        freshness("/scope/shared/documents/a.md", {}, _NOW),
        freshness(
            "/scope/shared/documents/b.md",
            {"last_verified_at": (_NOW - timedelta(days=31)).isoformat()},
            _NOW,
        ),
    ]

    counts = Counter(row["state"] for row in rows)
    assert counts == {"never_verified": 1, "overdue": 1}
    assert format_freshness(rows[0], "Documents & Artifacts") == (
        "Owner: Documents & Artifacts · never verified"
    )
    assert format_freshness(rows[1], "Documents & Artifacts") == (
        "Owner: Documents & Artifacts · verified 31 days ago · review overdue"
    )


def test_workspace_standard_cadence_row_matches_freshness_table():
    standard = (
        Path(__file__).parents[2]
        / "docs/contributing/bootstrap/starter-kit/shared/standards/WORKSPACE-STANDARD.md"
    )
    # Find the cadence row by its label so an unrelated row insertion cannot fail it.
    (line,) = [
        row for row in standard.read_text(encoding="utf-8").splitlines()
        if row.startswith("| How often is it checked? |")
    ]
    patterns = {
        "standard": r"Charters, standards, contracts: (\d+) days",
        "prd_rfc": r"Current PRDs and RFCs: (\d+) days",
        "domain_reference": r"Domain references and runbooks: (\d+) days",
        "draft": r"Drafts: (\d+) days",
    }
    parsed = {
        key: int(re.search(pattern, line).group(1))
        for key, pattern in patterns.items()
    }

    assert parsed == CADENCE_DAYS
    assert "Dated evidence: never audited" in line
