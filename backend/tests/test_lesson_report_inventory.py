"""Historical identity survives source changes without inventing activation/state."""
from __future__ import annotations

import pytest

from core.lesson_files import _with_lesson, render_trial, with_review_ref
from core.lesson_report_inventory import build_inventory, source_record
from core.lesson_trials import Trial

NOW = "2026-10-02T12:00:00Z"
SUBJECT = "6680cac5-3aa2-4a88-b491-352459567323"


def source(*, state="trial", review_after="2026-10-01", text="Check before deciding."):
    raw = render_trial(
        lesson_id=SUBJECT, owner="owner", name="deciding", description="Use before deciding.",
        text=text, trial=Trial("hypothesis", review_after),
        source_refs=[{"kind": "message", "reference": "correction"}],
    )
    raw = with_review_ref(raw, "review")
    if state == "promoted":
        raw = _with_lesson(raw, state=state, destination={"path": "skills/standing/SKILL.md"})
    elif state != "trial":
        raw = _with_lesson(raw, state=state)
    record = source_record(raw, "/authorized/skills/deciding/SKILL.md")
    assert record
    return record


def review(record, *, node="review", opened=True):
    return {
        "kind": "lesson", "subject_id": record["id"], "subject_hash": record["version"],
        "node_id": node, "owner_id": "owner", "open": opened,
        "close_reason": None if opened else "Retire it", "archived_at": None,
    }


def inventory(history=(), sources=(), retained=()):
    return build_inventory(history=list(history), sources=list(sources), retained=list(retained), now=NOW)


@pytest.mark.parametrize("review_after", ["2020-01-01", "2020-01-01T00:30:00Z"])
def test_current_source_preserves_calendar_date_or_timestamp_precision(review_after):
    record = source(review_after=review_after)
    assert record["review_after"] == review_after
    [row] = inventory(sources=[record])
    assert row["review_after"] == review_after
    assert row["due"] is True


@pytest.mark.parametrize("state", ["trial", "retired", "promoted"])
def test_current_state_and_due_work_are_not_inferred_from_review_text(state):
    current = source(state=state)
    [row] = inventory([review(current, opened=False), review(current, node="again")], [current])
    assert row["activated"] is True
    assert row["state"] == state
    assert row["due"] == (state == "trial")
    assert row["open_reviews"] == 1
    assert len(row["reviews"]) == 2
    assert row["current_source"]["raw_sha256"] == current["raw_sha256"]


def test_review_without_activation_edit_is_kept_with_unknown_activation():
    current = source()
    [row] = inventory([review(current, opened=False)])
    assert row["activated"] is None
    assert row["state"] is None  # A free-text "Retire it" is not a lifecycle transition.
    assert row["source_status"] == "unavailable"
    assert row["due"] is None


def test_old_version_survives_changed_source_and_genuine_offer_proves_activation():
    old, current = source(), source(text="A different behavioral rule.")
    rows = inventory([review(old)], [current], [{"kind": "lesson", "id": old["id"],
        "version": old["version"], "offered": True, "usage_id": "call", "owner_id": "deleted"}])
    previous = next(row for row in rows if row["version"] == old["version"])
    assert previous["activated"] is True
    assert previous["source_status"] == "changed"
    assert previous["current_source"] is None
    assert previous["state"] is None
    assert previous["retained"][0]["owner_id"] == "deleted"
    assert len(rows) == 2


def test_an_administrative_variant_does_not_become_an_arbitrary_offer_cost_or_path():
    first, extended = source(), source(review_after="2026-11-01")
    assert first["version"] == extended["version"]
    assert first["raw_sha256"] != extended["raw_sha256"]
    [row] = inventory([review(first)], [first, extended])
    assert row["source_status"] == "ambiguous"
    assert row["current_source"] is None
    assert row["due"] is None
    assert row["extension_history_complete"] is False


def test_capture_review_is_separate_and_a_body_read_is_not_activation():
    current = source()
    capture = {**review(current), "kind": "capture", "subject_id": "capture"}
    rows = inventory([capture], retained=[{"kind": "lesson", "id": current["id"],
        "version": current["version"], "guidance_load_id": "read", "offered": False}])
    assert next(row for row in rows if row["kind"] == "capture")["activated"] is False
    assert next(row for row in rows if row["kind"] == "lesson")["activated"] is None


def test_duplicate_review_identity_and_node_do_not_duplicate_the_denominator():
    current = source()
    repeated = review(current)
    [row] = inventory([repeated, repeated, review(current, node="second")], [current])
    assert len(row["reviews"]) == 2
    assert row["activated"] is True


def test_missing_source_keeps_known_review_due_work_separate_from_lifecycle():
    current = source()
    past = {**review(current), "scheduled_for": "2026-10-01T12:00:00Z"}
    future = {**review(current, node="later"), "scheduled_for": "2026-11-01T12:00:00Z"}
    [row] = inventory([past, future])
    assert row["state"] is None and row["due"] is None
    assert row["review_due"] is True and row["due_work"] is True
    assert row["due_review_nodes"] == ["review"]
    assert row["review_due_unknown"] == 0


def test_an_open_review_without_a_schedule_is_unresolved_not_assumed_due():
    current = source(state="retired")
    [row] = inventory([review(current)], [current])
    assert row["due"] is False
    assert row["review_due"] is None and row["due_work"] is None
    assert row["review_due_unknown"] == 1


def test_settled_review_does_not_add_due_work_to_a_retired_source():
    current = source(state="retired")
    [row] = inventory([{**review(current, opened=False), "scheduled_for": "2026-10-01"}], [current])
    assert row["review_due"] is False and row["due_work"] is False
    assert row["due_review_nodes"] == []


def test_due_order_ignores_a_period_without_evidence():
    due = source()
    later = {**source(review_after="2026-11-01"), "id": "later"}
    rows = inventory(sources=[later, due])
    assert [row["id"] for row in rows] == [SUBJECT, "later"]
    assert rows[0]["due"] is True
    assert rows[0]["retained"] == []
