"""A conductor reads one lifecycle state at a time, each row carrying a one-line statement."""
from __future__ import annotations

import json
import sqlite3
import uuid

import pytest

from core.lesson_files import _with_lesson, read_trial, render_trial, trial_statement, with_review_ref
from core.lesson_report import ReportScope, read_report
from core.lesson_report_inventory import source_record
from core.lesson_trials import Trial
from tests.test_lesson_review_history import db  # noqa: F401 - shared real-schema fixture
from tools.lesson_report import LessonReportTool

NOW = "2026-10-02T12:00:00Z"
SCOPE = ReportScope("user", ("owner",), "self", "2026-09-21", "2026-09-28")


def source(number: int, state: str, text: str = "Check before deciding."):
    raw = render_trial(
        lesson_id=str(uuid.UUID(int=number)), owner="owner", name=f"lesson-{number}",
        description="Use before deciding.", text=text, trial=Trial("hypothesis", "2026-10-01", state=state),
        source_refs=[{"kind": "message", "reference": "correction"}],
    )
    raw = with_review_ref(raw, f"review-{number}")
    if state == "retired":
        raw = _with_lesson(raw, state=state)
    record = source_record(raw, f"/authorized/skills/lesson-{number}/SKILL.md", with_statement=True)
    assert record
    return record


def report(db, sources, **options):
    return read_report(db, scope=SCOPE, sources=list(sources), now=NOW, **options)


def pages(db, sources, **options):
    """Every row reached by following next_cursor, with the page count."""
    rows, cursor, count = [], None, 0
    while True:
        page = report(db, sources, cursor=cursor, **options)
        rows += page["rows"]
        count += 1
        cursor = page["next_cursor"]
        if cursor is None:
            return rows, count


def mixed(each: int = 25):
    """Interleaved watch, trial and retired lessons."""
    return [source(number * 3 + offset, state)
            for number in range(each) for offset, state in enumerate(("watch", "trial", "retired"))]


def test_state_filter_returns_only_that_state_and_omitted_returns_everything(db):
    sources = [source(1, "watch"), source(2, "trial"), source(3, "retired"), source(4, "watch")]
    assert {row["state"] for row in report(db, sources, state="watch")["rows"]} == {"watch"}
    assert len(report(db, sources, state="watch")["rows"]) == 2
    assert [row["state"] for row in report(db, sources, state="trial")["rows"]] == ["trial"]
    everything = report(db, sources)
    assert everything["known_versions"] == 4
    assert report(db, sources, state=None)["rows"] == everything["rows"]


def test_filtered_paging_reaches_every_filtered_row_once_and_leaks_no_other_state(db):
    sources = mixed()
    for state in ("watch", "trial"):
        rows, count = pages(db, sources, state=state)
        expected = {record["id"] for record in sources if record["state"] == state}
        assert count == 2 and len(expected) == 25
        assert {row["id"] for row in rows} == expected and len(rows) == 25
        assert {row["state"] for row in rows} == {state}
        assert report(db, sources, state=state)["known_versions"] == 25
    assert len(pages(db, sources)[0]) == 75


def test_a_cursor_cannot_move_between_filtered_and_unfiltered_reads(db):
    sources = mixed()
    cursor = report(db, sources, state="watch")["next_cursor"]
    with pytest.raises(ValueError, match="scope differs"):
        report(db, sources, state="trial", cursor=cursor)
    with pytest.raises(ValueError, match="scope differs"):
        report(db, sources, cursor=cursor)


def test_an_unknown_state_is_refused(db):
    with pytest.raises(ValueError, match="state must be watch or trial"):
        report(db, [], state="retired")


def test_statement_is_from_a_real_canonical_trial_file_on_watch_and_trial_rows_only(db):
    sources = [source(1, "watch", "Never push a head the gates have not passed."),
               source(2, "trial", "Ask once, then proceed."), source(3, "retired", "Old rule.")]
    by_state = {row["state"]: row for row in report(db, sources)["rows"]}
    assert by_state["watch"]["statement"] == "Never push a head the gates have not passed."
    assert by_state["trial"]["statement"] == "Ask once, then proceed."
    assert "statement" not in by_state["retired"]


def test_statement_uses_the_canonical_trial_body_shape():
    raw = render_trial(
        lesson_id=str(uuid.UUID(int=8)), owner="owner", name="canonical",
        description="Use before deciding.", text="One canonical statement.",
        trial=Trial("hypothesis", "2026-10-01", boundary="Not for an unreviewed change."),
        source_refs=[{"kind": "message", "reference": "correction"}],
    )
    parsed = read_trial(raw)
    assert parsed
    assert parsed.statement == "One canonical statement."
    assert trial_statement(parsed.body.rstrip("\n")) == "One canonical statement."
    assert trial_statement(parsed.body.rstrip("\n") + "\nextra") == (
        "# Trial lesson An unproven hypothesis under review, not a standing rule. "
        "It grants no new permission. - One canonical statement. "
        "Does not apply: Not for an unreviewed change. extra"
    )


def test_valid_hand_edited_trial_body_is_a_visible_statement(db):
    hand_edited = render_trial(
        lesson_id=str(uuid.UUID(int=7)), owner="owner", name="lesson-7",
        description="Use before deciding.", text="Placeholder.",
        trial=Trial("hypothesis", "2026-10-01", state="watch"),
        source_refs=[{"kind": "message", "reference": "correction"}],
    ).replace("# Trial lesson", "# Hand-edited lesson", 1)
    record = source_record(hand_edited, "/authorized/skills/lesson-7/SKILL.md", with_statement=True)
    assert record and record["statement"] == (
        "# Hand-edited lesson An unproven hypothesis under review, not a standing rule. "
        "It grants no new permission. - Placeholder."
    )
    [row] = report(db, [record])["rows"]
    assert row["state"] == "watch"
    assert row["statement"] == record["statement"]
    assert "statement_error" not in row


@pytest.mark.asyncio
async def test_tool_filters_real_lesson_files_by_state_and_reports_their_statements(isolated_db, tmp_path):
    workspace = tmp_path / "workspace"
    conn = sqlite3.connect(isolated_db)
    conn.execute("INSERT INTO threads(id,user_id,agent_enabled,model,created_at,updated_at) "
                 "VALUES('owner','user',1,'test/model','t','t')")
    conn.commit()
    conn.close()
    for number, (state, text) in enumerate([("watch", "Never skip the check."), ("trial", "Ask once.")]):
        raw = render_trial(
            lesson_id=str(uuid.UUID(int=number + 1)), owner="owner", name=f"file-{state}",
            description="Use before deciding.", text=text, trial=Trial("hypothesis", "2026-12-01", state=state),
            source_refs=[{"kind": "message", "reference": "correction"}])
        path = workspace / ".threads" / "owner" / "skills" / f"file-{state}" / "SKILL.md"
        path.parent.mkdir(parents=True)
        path.write_text(with_review_ref(raw, f"review-{number}"))
    tool = LessonReportTool(workspace)
    assert "state" in tool.parameters["properties"]
    for state, statement in (("watch", "Never skip the check."), ("trial", "Ask once.")):
        result = json.loads(await tool.execute(_thread_id="owner", state=state))
        assert [(row["state"], row["statement"]) for row in result["rows"]] == [(state, statement)]
    assert json.loads(await tool.execute(_thread_id="owner"))["known_versions"] == 2
    assert "state must be" in json.loads(await tool.execute(_thread_id="owner", state="promoted"))["error"]
