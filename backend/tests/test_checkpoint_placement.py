"""The checkpoint placement pass: fork review, marks, and the placement worker."""

from __future__ import annotations

import json
import sqlite3
import subprocess
from pathlib import Path

import pytest

from tests.test_continuity_staging import (
    _capture_install,
    _connect,
    _db,
    _running,
    _seed_thread,
    _tool_db,
)


def _lesson(text: str, refs: list[dict] | None = None) -> dict:
    return {"statement": text, "source_refs": refs if refs is not None else [
        {"kind": "user", "reference": "said so in chat"},
    ]}


def _add(db, field: str, body: dict, thread_id: str = "thread-1") -> int:
    from infra.continuity_store import add_entry

    entry_id = add_entry(
        db, thread_id=thread_id, history_epoch=0, field=field, body=body,
    )
    db.commit()
    return entry_id


def _mark(db, entry_id: int, thread_id: str = "thread-1"):
    return db.execute(
        "SELECT state,revision,placement_state,placement_revision,placement_detail "
        "FROM continuity_entries WHERE thread_id=? AND entry_id=?",
        (thread_id, entry_id),
    ).fetchone()


# -- schema --------------------------------------------------------------------


def test_migration_40_adds_marks_and_admits_review_keeping_staged_rows() -> None:
    from infra.migration_39_to_40_placement_marks import migrate_39_to_40

    db = _connect()
    db.execute("CREATE TABLE checkpoints (id TEXT PRIMARY KEY)")
    db.execute(
        "CREATE TABLE continuity_entries (thread_id TEXT, history_epoch INTEGER,"
        " entry_id INTEGER, revision INTEGER)"
    )
    db.execute(
        "CREATE TABLE checkpoint_staged_edits ("
        " checkpoint_id TEXT NOT NULL REFERENCES checkpoints(id) ON DELETE CASCADE,"
        " edit_order INTEGER NOT NULL,"
        " operation TEXT NOT NULL CHECK (operation IN ('add','replace','drop')),"
        " entry_id INTEGER NOT NULL CHECK (entry_id > 0), field TEXT NOT NULL,"
        " body TEXT, reason TEXT, source_ref TEXT, base_entry_revision INTEGER,"
        " outcome TEXT NOT NULL DEFAULT 'pending', outcome_detail TEXT,"
        " created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP, resolved_at TEXT,"
        " PRIMARY KEY (checkpoint_id, edit_order))"
    )
    db.execute("INSERT INTO checkpoints VALUES ('cp')")
    db.execute(
        "INSERT INTO checkpoint_staged_edits (checkpoint_id,edit_order,operation,"
        "entry_id,field,reason) VALUES ('cp',1,'drop',3,'facts','stale')"
    )

    migrate_39_to_40(db)

    columns = {row[1] for row in db.execute("PRAGMA table_info(continuity_entries)")}
    assert {"placement_state", "placement_revision", "placement_detail"} <= columns
    kept = db.execute("SELECT operation,reason FROM checkpoint_staged_edits").fetchall()
    assert [tuple(row) for row in kept] == [("drop", "stale")]
    db.execute(
        "INSERT INTO checkpoint_staged_edits (checkpoint_id,edit_order,operation,"
        "entry_id,field) VALUES ('cp',2,'review',3,'decisions')"
    )
    with pytest.raises(sqlite3.IntegrityError):
        db.execute(
            "INSERT INTO continuity_entries (thread_id,history_epoch,entry_id,"
            "revision,placement_state) VALUES ('t',0,1,1,'bogus')"
        )


def test_migration_40_staging_copy_matches_the_live_schema_while_current() -> None:
    from infra.continuity_staging import STAGING_SCHEMA_SQL
    from infra.migration_39_to_40_placement_marks import STAGING_40_DDL
    from infra.schema import SCHEMA_VERSION

    if SCHEMA_VERSION == 158:
        assert STAGING_40_DDL == STAGING_SCHEMA_SQL


# -- fork review ---------------------------------------------------------------


def _review_setup(tmp_path, monkeypatch):
    path = tmp_path / "review.db"
    db = _db(str(path))
    _seed_thread(db)
    lesson = _add(db, "decisions", {**_lesson("prefer small commits"), "rationale": "r"})
    fact = _add(db, "facts", _lesson("a plain fact"))
    _running(db)
    db.commit()
    monkeypatch.setattr("infra.config.WORKSPACE", tmp_path)
    return path, db, lesson, fact


def test_review_block_asks_the_fork_where_unmarked_lessons_go(
    tmp_path, monkeypatch
) -> None:
    from core.placement_decide import GUIDE
    from core.placement_review import review_block

    _, db, lesson, fact = _review_setup(tmp_path, monkeypatch)
    monkeypatch.setattr(
        "core.placement_answer.history", lambda db, thread_id, entry: [],
    )
    block = review_block("cp-1", "thread-1", 8_000, db=db)
    assert block is not None
    assert GUIDE in block
    where = json.loads(block.split("Where it can go:\n", 1)[1].split("\n\nEntries:", 1)[0])
    assert set(where) == {"thread", "destinations"}
    offered = json.loads(block.split("Entries:\n", 1)[1])
    assert [item["entry_id"] for item in offered] == [lesson, fact]
    assert [item["kind"] for item in offered] == ["lesson", "work_state"]
    assert offered[0]["earlier_history"] == []
    assert "earlier_history" not in offered[1]


def test_review_block_skips_child_threads(tmp_path, monkeypatch) -> None:
    from core.placement_review import review_block

    _, db, _, _ = _review_setup(tmp_path, monkeypatch)
    _seed_thread(db, "parent")
    db.execute("UPDATE threads SET spawned_by='parent' WHERE id='thread-1'")
    assert review_block("cp-1", "thread-1", 8_000, db=db) is None


def test_staged_verdicts_publish_marks_only_when_the_checkpoint_installs(
    tmp_path, monkeypatch
) -> None:
    from infra.continuity_store import add_entry

    path, db, lesson, fact = _review_setup(tmp_path, monkeypatch)
    already = add_entry(
        db, thread_id="thread-1", history_epoch=0, field="known_failures",
        body={"result": "x", "evidence": "y", "source_refs": []},
    )
    db.commit()
    # A new checkpoint so the frozen view includes the third lesson.
    db.execute("DELETE FROM checkpoints")
    db.commit()
    _running(db)
    db.commit()
    db.close()
    continuity = _tool_db(path, monkeypatch)

    bare_select = continuity._run(
        "thread-1", "review", {"entry_id": lesson, "verdict": "select"}, "cp-1",
    )
    bad_answer = continuity._run(
        "thread-1", "review",
        {"entry_id": lesson, "verdict": "select", "placement": _raw(q1="galaxy")},
        "cp-1",
    )
    selected = continuity._run(
        "thread-1", "review",
        {"entry_id": lesson, "verdict": "select", "placement": _raw(q1="agent")},
        "cp-1",
    )
    kept_lesson = continuity._run(
        "thread-1", "review",
        {"entry_id": already, "verdict": "reaffirm", "reason": "still matters"},
        "cp-1",
    )
    no_reason = continuity._run(
        "thread-1", "review", {"entry_id": fact, "verdict": "reaffirm"}, "cp-1",
    )
    stray = continuity._run(
        "thread-1", "review",
        {"entry_id": fact, "verdict": "reaffirm", "reason": "still true",
         "placement": _raw()},
        "cp-1",
    )
    twice = continuity._run(
        "thread-1", "review", {"entry_id": lesson, "verdict": "reaffirm"}, "cp-1",
    )
    bare_already = continuity._run(
        "thread-1", "review",
        {"entry_id": already, "verdict": "already", "destination": "context/X.md"},
        "cp-1",
    )
    (tmp_path / "context").mkdir()
    (tmp_path / "context" / "X.md").write_text("# X\n\n- Keep commits small.\n")
    unsaid = continuity._run(
        "thread-1", "review",
        {"entry_id": already, "verdict": "already", "destination": "context/X.md",
         "quote": "Run every test first."},
        "cp-1",
    )
    done = continuity._run(
        "thread-1", "review",
        {"entry_id": already, "verdict": "already", "destination": "context/X.md",
         "quote": "- Keep commits small."},
        "cp-1",
    )
    live = continuity._run("thread-1", "review", {"entry_id": lesson, "verdict": "select"})

    assert bare_select == "Error: verdict select requires placement"
    assert bad_answer == "Error: q1 or q2 is outside the table"
    assert selected == f"Staged entry {lesson} verdict select."
    assert kept_lesson.startswith("Error: a lesson is never kept in continuity")
    assert no_reason.startswith("Error: verdict reaffirm requires reason")
    assert stray == "Error: placement and room go only with verdict select"
    assert "already has a staged edit" in twice
    assert bare_already == "Error: verdict already requires destination and quote"
    assert unsaid.startswith("Error: context/X.md does not contain the quoted line")
    assert done == f"Staged entry {already} verdict already."
    assert live == "Error: review runs only inside a checkpoint"

    db = _connect(str(path))
    assert _mark(db, lesson)["placement_state"] is None  # nothing before install
    _capture_install(db)
    db.commit()
    marked = _mark(db, lesson)
    assert (marked["placement_state"], marked["placement_revision"]) == (
        "selected", marked["revision"],
    )
    stored = json.loads(marked["placement_detail"])
    assert stored["checkpoint"] == "checkpoint:cp-1"
    assert stored["answer"] == {"placement": _raw(q1="agent"), "room": None}
    retired = _mark(db, already)
    assert (retired["state"], retired["placement_state"]) == ("retired", "already")
    assert retired["placement_detail"] == "context/X.md"


def test_checkpoint_stages_a_flagged_trial_with_its_checkpoint_epoch(tmp_path, monkeypatch) -> None:
    path, db, lesson, _ = _review_setup(tmp_path, monkeypatch)
    db.close()
    continuity = _tool_db(path, monkeypatch)
    trial = _raw(
        q2="task_start", text="On 2026-10-14, name the exact commit a check ran on.",
        skill_name="commit-check", skill_description="Record exact commits for check results.",
        trial={"kind": "hypothesis", "review_after": "2027-01-01", "boundary": None},
    )

    result = continuity._run(
        "thread-1", "review", {"entry_id": lesson, "verdict": "select", "placement": trial}, "cp-1",
    )

    assert result.startswith(f"Staged entry {lesson} verdict select")
    db = _connect(str(path))
    row = db.execute(
        "SELECT history_epoch,entry_id,revision FROM lesson_placement_checks"
    ).fetchone()
    assert tuple(row) == (0, lesson, 1)


def test_already_conflicts_when_the_file_stopped_saying_it_before_install(
    tmp_path, monkeypatch
) -> None:
    path, db, lesson, _ = _review_setup(tmp_path, monkeypatch)
    target = tmp_path / "context" / "X.md"
    target.parent.mkdir()
    target.write_text("- Keep commits small.\n")
    db.close()
    continuity = _tool_db(path, monkeypatch)
    staged = continuity._run(
        "thread-1", "review",
        {"entry_id": lesson, "verdict": "already", "destination": "context/X.md",
         "quote": "Keep commits small."},
        "cp-1",
    )
    target.write_text("- Something else entirely.\n")

    db = _connect(str(path))
    _capture_install(db)
    db.commit()

    assert staged == f"Staged entry {lesson} verdict already."
    kept = _mark(db, lesson)
    assert (kept["state"], kept["placement_state"]) == ("active", None)
    outcome = db.execute(
        "SELECT outcome FROM checkpoint_staged_edits WHERE entry_id=?", (lesson,),
    ).fetchone()
    assert outcome["outcome"] == "conflict"


def test_select_over_the_cap_asks_the_fork_for_room_in_the_same_loop(
    tmp_path, monkeypatch
) -> None:
    path, db, lesson, _ = _review_setup(tmp_path, monkeypatch)
    full = "# R\n\n## Lessons\n" + "".join(f"- lesson {n}\n" for n in range(5))
    context = tmp_path / ".threads" / "thread-1" / "context"
    context.mkdir(parents=True)
    (context / "RESPONSIBILITY.md").write_text(full)
    db.close()
    continuity = _tool_db(path, monkeypatch)
    params = {"entry_id": lesson, "verdict": "select", "placement": _raw()}

    asked = continuity._run("thread-1", "review", params, "cp-1")
    unasked = continuity._run(
        "thread-1", "review",
        {**params, "placement": _raw(q1="everywhere"), "room": []}, "cp-1",
    )
    unusable = continuity._run(
        "thread-1", "review",
        {**params, "room": [{"op": "remove", "start": 4, "end": None, "text": None,
                             "basis": None, "path": None, "quote": None}]},
        "cp-1",
    )
    fold = [{"op": "fold", "start": 4, "end": None, "text": _raw()["text"],
             "basis": None, "path": None, "quote": None}]
    staged = continuity._run("thread-1", "review", {**params, "room": fold}, "cp-1")

    assert asked.startswith("Error: this line would pass its destination's cap.")
    assert f"for entry {lesson} with the same placement and room" in asked
    assert "4: - lesson 0" in asked
    assert unasked.startswith("Error: room is only for a write that would pass its cap")
    assert unusable.startswith("Error: this room cannot apply to the numbered file (no-room)")
    assert f"for entry {lesson} with the same placement and corrected room" in unusable
    assert staged == f"Staged entry {lesson} verdict select."
    db = _connect(str(path))
    _capture_install(db)
    db.commit()
    stored = json.loads(_mark(db, lesson)["placement_detail"])["answer"]
    assert stored["room"]["base"] == full
    assert [op["op"] for op in stored["room"]["operations"]] == ["fold"]


def test_select_with_empty_room_stages_the_honest_no_room(tmp_path, monkeypatch) -> None:
    path, db, lesson, _ = _review_setup(tmp_path, monkeypatch)
    full = "# R\n\n## Lessons\n" + "".join(f"- lesson {n}\n" for n in range(5))
    context = tmp_path / ".threads" / "thread-1" / "context"
    context.mkdir(parents=True)
    (context / "RESPONSIBILITY.md").write_text(full)
    db.close()
    continuity = _tool_db(path, monkeypatch)

    staged = continuity._run(
        "thread-1", "review",
        {"entry_id": lesson, "verdict": "select", "placement": _raw(), "room": []},
        "cp-1",
    )

    assert staged == f"Staged entry {lesson} verdict select."
    db = _connect(str(path))
    _capture_install(db)
    db.commit()
    stored = json.loads(_mark(db, lesson)["placement_detail"])["answer"]
    assert stored["room"] == {"base": full, "operations": []}


def _lesson_skill(tmp_path, body: str, description: str = "Use when merging.") -> Path:
    """A trial-lesson skill in thread-1's tray, as lesson_files writes one."""
    skill = tmp_path / ".threads" / "thread-1" / "skills" / "gate" / "SKILL.md"
    skill.parent.mkdir(parents=True)
    skill.write_text(
        f"---\nname: gate\ndescription: {description}\ncorsia:\n  lesson:\n"
        "    id: 9ece0e89-3e4d-5e77-b235-d678598db53b\n    state: trial\n"
        "    owner: thread-1\n    review_after: '2026-10-22T00:00:00+00:00'\n"
        "    source_refs:\n    - kind: turn\n      reference: turn-1\n"
        f"---\n\n{body}"
    )
    return skill


def test_a_lesson_skill_asks_for_room_at_the_lesson_cap(tmp_path, monkeypatch) -> None:
    path, db, lesson, _ = _review_setup(tmp_path, monkeypatch)
    long_line = "- " + " ".join(["word"] * 140)
    skill = _lesson_skill(tmp_path, f"# Trial lesson\n\n{long_line}\n")
    db.close()
    continuity = _tool_db(path, monkeypatch)
    params = {"entry_id": lesson, "verdict": "select",
              "placement": _raw(q2="task_start", skill_name="gate")}

    asked = continuity._run("thread-1", "review", params, "cp-1")
    number = skill.read_text().split("\n").index(long_line) + 1
    fold = [{"op": "fold", "start": number, "end": None, "text": _raw()["text"],
             "basis": None, "path": None, "quote": None}]
    staged = continuity._run("thread-1", "review", {**params, "room": fold}, "cp-1")

    assert asked.startswith("Error: this line would pass its destination's cap.")
    assert "body is 152 words; cap 150" in asked
    assert staged == f"Staged entry {lesson} verdict select."


def test_a_skill_write_the_loader_refuses_goes_back_to_the_fork(
    tmp_path, monkeypatch
) -> None:
    path, db, lesson, _ = _review_setup(tmp_path, monkeypatch)
    _lesson_skill(tmp_path, "# Trial lesson\n\n- one rule\n",
                  description=" ".join(["Use"] * 41))
    db.close()
    continuity = _tool_db(path, monkeypatch)

    refused = continuity._run(
        "thread-1", "review",
        {"entry_id": lesson, "verdict": "select",
         "placement": _raw(q2="task_start", skill_name="gate")},
        "cp-1",
    )

    assert refused.startswith(
        "Error: the skill would not load after this write "
        "(a lesson description is at most 40 words)."
    )
    assert f"for entry {lesson} with another destination" in refused


def _answered() -> str:
    """A selected mark's detail, as an install stores it."""
    return json.dumps({"checkpoint": "checkpoint:cp-1",
                       "answer": {"placement": _raw(), "room": None}})


def test_a_rewrite_unmarks_the_entry(tmp_path, monkeypatch) -> None:
    from infra.continuity_store import replace_entry
    from infra.placement_marks import selected_entries, set_mark

    db = _db()
    _seed_thread(db)
    entry = _add(db, "decisions", {**_lesson("one"), "rationale": "r"})
    revision = _mark(db, entry)["revision"]
    set_mark(db, thread_id="thread-1", history_epoch=0, entry_id=entry,
             state="selected", revision=revision,
             detail=_answered())
    assert [row["entry_id"] for row in selected_entries(db)] == [entry]
    replace_entry(db, thread_id="thread-1", history_epoch=0, entry_id=entry,
                  body={**_lesson("one, revised"), "rationale": "r"})
    assert selected_entries(db) == []


def test_an_earlier_epoch_selection_is_neither_queued_nor_counted() -> None:
    from infra.placement_marks import outstanding_count, selected_entries, set_mark

    db = _db()
    _seed_thread(db)
    entry = _add(db, "decisions", {**_lesson("one"), "rationale": "r"})
    set_mark(db, thread_id="thread-1", history_epoch=0, entry_id=entry,
             state="selected", revision=_mark(db, entry)["revision"],
             detail=_answered())
    assert (outstanding_count(db), len(selected_entries(db))) == (1, 1)
    db.execute("UPDATE threads SET history_epoch=1 WHERE id='thread-1'")
    assert (outstanding_count(db), selected_entries(db)) == (0, [])


# -- decisions and proposals ---------------------------------------------------


def _raw(**overrides) -> dict:
    raw = {
        "q1": "agent", "q2": "every_turn", "duplicate_of": None,
        "text": "Run the smallest affected tests before pushing.",
        "skill_name": None, "skill_description": None, "reference_file": None,
        "rationale": "applies to this lane every turn",
    }
    raw.update(overrides)
    return raw


def _decision(**overrides):
    from core.placement_decide import validate_decision

    return validate_decision(_raw(**overrides))


def _place_for(tmp_path, *, agent: bool = True, reference: bool = False):
    from core.placement_decide import ThreadPlace

    tray = tmp_path / ".threads" / "t1"
    tray.mkdir(parents=True, exist_ok=True)
    ref = None
    if reference:
        ref = tmp_path / "shared" / "lane"
        ref.mkdir(parents=True, exist_ok=True)
        (ref / "NOTES.md").write_text("# Notes\n")
    return ThreadPlace("t1", "Lane", "p1", agent, tray, ref)


def test_validate_decision_refuses_answers_outside_the_table() -> None:
    from core.placement_decide import InvalidDecision

    with pytest.raises(InvalidDecision):
        _decision(q1="galaxy")
    with pytest.raises(InvalidDecision):
        _decision(text="  ")
    assert _decision(text="a\n  b").text == "a b"


def test_case_metadata_is_optional_and_refused_before_it_is_stored() -> None:
    from core import placement_answer
    from core.placement_decide import InvalidDecision

    assert _decision().case_metadata == {}
    case = {
        "what_happened": "A placement case was lost.",
        "right_call": "Keep it with the lesson.",
        "does_not_apply": "A lesson with no case.",
    }
    decision = _decision(case=case, cause="rule_gap", target_file=" context/rule.md ", new_target_path=None)
    assert decision.case_metadata == {"target_file": "context/rule.md", "cause": "rule_gap", "case": case}
    assert placement_answer.load({"placement": placement_answer._stored(decision)})[0] == decision

    actions = {
        "required_actions": [{"tool": "read_file", "params": {}}],
        "forbidden_actions": [],
        "decisive_action": {"tool": "write_file", "params": {}},
    }
    for invalid, message in (
        ({"cause": "bad_luck"}, "cause must be one of wrong_rule, no_foundation, rule_gap"),
        ({"new_evidence": "unsupported"}, "new_evidence must be one of target_changed, case_differs"),
        ({"case": {**case, "correction_subject": "continuity-entry:1"}}, "case may name only"),
        ({"case": {**case, "actions": actions}}, "decisive_action must be one declared"),
        ({"target_file": 3}, "target_file must be a string or null"),
        ({"target_file": "context/rule.md", "new_target_path": "context/new.md"},
         "target_file and new_target_path do not go together"),
    ):
        with pytest.raises(InvalidDecision, match=message):
            _decision(**invalid)


def test_destination_mapping_never_leaves_the_tray_or_reference(tmp_path) -> None:
    from core.placement_decide import InvalidDecision, map_destination

    place = _place_for(tmp_path, reference=True)
    context = map_destination(place, _decision())
    assert context.path == place.tray / "context" / "RESPONSIBILITY.md"
    plain = map_destination(_place_for(tmp_path, agent=False), _decision())
    assert plain.path == place.tray / "context" / "LESSONS.md"
    skill = map_destination(place, _decision(q2="task_start", skill_name="push-checks"))
    assert skill.path == place.tray / "skills" / "push-checks" / "SKILL.md"
    with pytest.raises(InvalidDecision):
        map_destination(place, _decision(q2="task_start", skill_name="../../evil"))
    reference = map_destination(place, _decision(q2="lookup", reference_file="NOTES.md"))
    assert reference.kind == "reference"
    escape = map_destination(place, _decision(q2="lookup", reference_file="../x.md"))
    assert (escape.kind, escape.reason) == ("stays", "no-agent-reference")
    assert map_destination(place, _decision(q1="project")).kind == "proposal"
    assert map_destination(place, _decision(q1="thread")).kind == "stays"


def test_append_lesson_counts_lessons_in_its_own_section() -> None:
    from core.placement_decide import append_lesson

    text = "# R\n\n## Lessons\n- a\n- b\n\n## Other\n- c\n"
    updated, count = append_lesson(text, "new", "2026-09-24")
    assert count == 3
    assert "- b\n- 2026-09-24: new\n\n## Other" in updated
    fresh, one = append_lesson(None, "first", "2026-09-24")
    assert one == 1 and fresh.startswith("# Lessons\n")


def test_p3_refuses_verbatim_runs_and_the_users_name() -> None:
    from core.placement_proposal import p3_violation

    source = "Robin said always run the smallest affected tests before pushing"
    assert p3_violation("run the smallest affected tests before pushing now", source, [])
    assert p3_violation("Ask Robin first", "unrelated", ["Robin"])
    assert not p3_violation("Verify narrowly before a push.", source, ["Robin"])


def test_p3_refuses_short_verbatim_candidates() -> None:
    from core.placement_proposal import p3_violation

    source = "Review tests before publishing proposed rules"
    assert p3_violation("Review tests", source, [])


def test_file_issue_is_idempotent_and_prefixes_workspace(monkeypatch) -> None:
    from core import placement_proposal

    calls = []

    def fake_gh(args, *, input_text=None, timeout=30.0):
        calls.append((args, input_text))
        if args[:2] == ["issue", "list"]:
            body = placement_proposal.marker("t1", 4, 9) if len(calls) > 2 else ""
            payload = [{"url": "https://x/1", "body": body}] if body else []
            return subprocess.CompletedProcess(args, 0, json.dumps(payload), "")
        return subprocess.CompletedProcess(args, 0, "https://x/2\n", "")

    monkeypatch.setattr("reviewer.gh_api.run_gh", fake_gh)
    kwargs = dict(repo="o/r", label="lane:conductor", thread_id="t1", entry_id=4,
                  revision=9, q1="everywhere", q2="task_start",
                  rule="Rule.", rationale="Why.")
    created = placement_proposal.file_issue(**kwargs)
    assert (created.outcome, created.url) == ("proposed", "https://x/2")
    title = calls[1][0][calls[1][0].index("--title") + 1]
    assert title.startswith("[workspace] ")
    assert placement_proposal.marker("t1", 4, 9) in calls[1][1]
    again = placement_proposal.file_issue(**kwargs)
    assert (again.reason, again.url) == ("proposed-existing", "https://x/1")


# -- guidance commits ----------------------------------------------------------


def _repo(tmp_path) -> Path:
    repo = tmp_path / "repo"
    repo.mkdir()
    for args in (
        ["init", "-q"], ["config", "user.email", "t@t"], ["config", "user.name", "t"],
    ):
        subprocess.run(["git", "-C", str(repo), *args], check=True)
    (repo / "keep.md").write_text("keep\n")
    subprocess.run(["git", "-C", str(repo), "add", "."], check=True)
    subprocess.run(["git", "-C", str(repo), "commit", "-qm", "base"], check=True)
    return repo


def _log(repo: Path) -> list[str]:
    return subprocess.run(
        ["git", "-C", str(repo), "log", "--format=%s"], capture_output=True,
        text=True, check=True,
    ).stdout.split("\n")[:-1]


def _placement_save(workspace: Path, target: Path, text: str, expected: str | None):
    from infra.guidance_git import digest, save_guidance
    from infra.guidance_targets import GuidanceTarget

    return save_guidance(
        {target: text.encode()}, workspace=workspace, target=GuidanceTarget("placement"),
        expected={target: digest(expected)}, atomic=True, author="Lane <t@a>",
        message="placement: written\n\nPlacement: t/1@2\n",
    )


def test_placement_save_commits_its_path_after_recording_outside_edits(tmp_path) -> None:
    from infra.guidance_git import find_commit

    repo = _repo(tmp_path)
    (repo / "keep.md").write_text("someone else's edit\n")
    target = repo / "context" / "LESSONS.md"
    result = _placement_save(repo, target, "# Lessons\n- x\n", None)
    assert result.outcome == "written"
    changed = subprocess.run(
        ["git", "-C", str(repo), "show", "--name-only", "--format=", "HEAD"],
        capture_output=True, text=True, check=True,
    ).stdout.split()
    assert changed == ["context/LESSONS.md"]
    assert (repo / "keep.md").read_text() == "someone else's edit\n"
    assert find_commit(repo, "Placement: t/1@2", target) == result.sha

    moved = _placement_save(repo, target, "other", "stale")
    assert (moved.outcome, moved.reason) == ("stale", "deferred-destination-changed")
    assert target.read_text() == "# Lessons\n- x\n"
    # An outside edit on the destination is recorded first, never folded in.
    target.write_text("# Lessons\n- x\n- outside\n")
    after = _placement_save(repo, target, "# Lessons\n- x\n- outside\n- y\n", target.read_text())
    assert after.outcome == "written"
    assert _log(repo) == [
        "placement: written", "guidance sweep: context/LESSONS.md", "placement: written", "base",
    ]
    swept = subprocess.run(
        ["git", "-C", str(repo), "show", "HEAD~1:context/LESSONS.md"],
        capture_output=True, text=True, check=True,
    ).stdout
    assert swept == "# Lessons\n- x\n- outside\n"


def test_placement_save_inside_an_enclosing_repository_writes_nothing(tmp_path) -> None:
    workspace = _repo(tmp_path) / "inner"
    target = workspace / "context" / "x.md"
    result = _placement_save(workspace, target, "x", None)
    assert result.outcome == "history-off"
    assert not target.exists()


# -- the worker ----------------------------------------------------------------


@pytest.fixture
def worker_env(tmp_path, monkeypatch):
    from core import placement_worker
    from infra.placement_marks import set_mark

    workspace = _repo(tmp_path)
    path = tmp_path / "worker.db"
    db = _db(str(path))
    _seed_thread(db)
    db.execute("UPDATE threads SET title='Lane', agent_enabled=1 WHERE id='thread-1'")
    db.commit()
    monkeypatch.setattr("infra.database.get_db", lambda: _connect(str(path)))
    monkeypatch.setattr("infra.config.WORKSPACE", workspace)
    tray = workspace / ".threads" / "thread-1"
    monkeypatch.setattr(
        placement_worker, "_thread_place",
        lambda thread_id: _place_for_worker(thread_id, tray),
    )
    monkeypatch.setattr(
        "core.context_source_paths.append_context_source_change",
        lambda *a, **k: None,
    )

    def select(
        body: dict,
        field: str = "decisions",
        *,
        placement: dict | None = None,
        room: dict | None = None,
    ) -> int:
        """Mark an entry selected with the fork's answer, as an install does."""
        entry = _add(db, field, body)
        revision = _mark(db, entry)["revision"]
        detail = json.dumps({
            "checkpoint": "checkpoint:cp-1",
            "answer": {"placement": placement or _raw(), "room": room},
        })
        set_mark(db, thread_id="thread-1", history_epoch=0, entry_id=entry,
                 state="selected", revision=revision, detail=detail)
        db.commit()
        return entry

    return placement_worker, db, workspace, tray, select, monkeypatch


def _place_for_worker(thread_id: str, tray: Path):
    from core.placement_decide import ThreadPlace

    return ThreadPlace(thread_id, "Lane", None, True, tray, None)


async def _drain(worker) -> None:
    worker.poke()
    await worker.wait_idle()


async def test_worker_writes_commits_and_retires_the_entry(worker_env) -> None:
    worker, db, workspace, tray, select, monkeypatch = worker_env
    entry = select({**_lesson("small commits"), "rationale": "r"})

    await _drain(worker)

    responsibility = tray / "context" / "RESPONSIBILITY.md"
    assert "Run the smallest affected tests" in responsibility.read_text()
    assert _log(workspace)[0] == "placement: written .threads/thread-1/context/RESPONSIBILITY.md"
    marked = _mark(db, entry)
    assert (marked["state"], marked["placement_state"]) == ("retired", "written")
    assert "RESPONSIBILITY.md" in marked["placement_detail"]


async def test_worker_refuses_an_entry_with_no_source(worker_env) -> None:
    worker, db, _, _, select, _ = worker_env
    entry = select({**_lesson("no source", refs=[]), "rationale": "r"})

    await _drain(worker)

    marked = _mark(db, entry)
    assert (marked["state"], marked["placement_state"]) == ("active", "refused")


async def test_worker_records_an_outside_edit_before_placing_on_it(worker_env) -> None:
    worker, db, workspace, tray, select, _ = worker_env
    context = tray / "context"
    context.mkdir(parents=True)
    (context / "RESPONSIBILITY.md").write_text("# R\nuncommitted\n")
    entry = select({**_lesson("small commits"), "rationale": "r"})

    await _drain(worker)

    marked = _mark(db, entry)
    assert (marked["state"], marked["placement_state"]) == ("retired", "written")
    assert (context / "RESPONSIBILITY.md").read_text().startswith("# R\nuncommitted\n")
    assert _log(workspace)[1].startswith("guidance sweep: .threads/thread-1/context/RESPONSIBILITY.md")


async def test_only_a_transient_deferral_keeps_the_selection(worker_env) -> None:
    from core.placement_outcomes import defer

    _, db, _, _, select, _ = worker_env
    held, stuck = (
        select({**_lesson(text), "rationale": "r"}) for text in ("held", "stuck")
    )
    for entry_id, reason in ((held, "deferred-index-locked"),
                             (stuck, "deferred-github-unavailable")):
        defer({"thread_id": "thread-1", "history_epoch": 0, "entry_id": entry_id,
               "revision": _mark(db, entry_id)["revision"]}, reason)

    assert _mark(db, held)["placement_state"] == "selected"
    assert (_mark(db, stuck)["state"], _mark(db, stuck)["placement_state"]) == (
        "active", None,
    )


async def test_worker_error_ends_the_selection(worker_env) -> None:
    worker, db, _, _, select, monkeypatch = worker_env
    entry = select({**_lesson("breaks the worker"), "rationale": "r"})

    def broken(thread_id):
        raise RuntimeError("boom")

    monkeypatch.setattr(worker, "_thread_place", broken)

    await _drain(worker)

    assert (_mark(db, entry)["state"], _mark(db, entry)["placement_state"]) == (
        "active", None,
    )


async def test_worker_leaves_over_cap_lessons_unmarked(worker_env) -> None:
    worker, db, workspace, tray, select, _ = worker_env
    context = tray / "context"
    context.mkdir(parents=True)
    (context / "RESPONSIBILITY.md").write_text(
        "# R\n\n## Lessons\n" + "".join(f"- lesson {n}\n" for n in range(5))
    )
    subprocess.run(["git", "-C", str(workspace), "add", "."], check=True)
    subprocess.run(["git", "-C", str(workspace), "commit", "-qm", "seed"], check=True)
    entry = select({**_lesson("small commits"), "rationale": "r"})

    await _drain(worker)

    marked = _mark(db, entry)
    assert (marked["state"], marked["placement_state"]) == ("active", None)
    assert _log(workspace)[0] == "seed"


async def test_worker_settles_an_empty_room_as_logged_no_room(worker_env) -> None:
    worker, db, workspace, tray, select, monkeypatch = worker_env
    full = "# R\n\n## Lessons\n" + "".join(f"- lesson {n}\n" for n in range(5))
    context = tray / "context"
    context.mkdir(parents=True)
    (context / "RESPONSIBILITY.md").write_text(full)
    subprocess.run(["git", "-C", str(workspace), "add", "."], check=True)
    subprocess.run(["git", "-C", str(workspace), "commit", "-qm", "seed"], check=True)
    entry = select(
        {**_lesson("small commits"), "rationale": "r"},
        room={"base": full, "operations": []},
    )
    seen: list[tuple[str, str]] = []
    monkeypatch.setattr(
        "core.placement_outcomes.log_outcome",
        lambda **kwargs: seen.append((kwargs["outcome"], kwargs["reason"])),
    )

    await _drain(worker)

    assert seen == [("unplaced", "no-room")]
    assert _log(workspace)[0] == "seed"
    assert (context / "RESPONSIBILITY.md").read_text() == full


async def test_worker_keeps_thread_lessons_in_continuity(worker_env) -> None:
    worker, db, _, _, select, _ = worker_env
    entry = select({**_lesson("only here"), "rationale": "r"}, placement=_raw(q1="thread"))

    await _drain(worker)

    marked = _mark(db, entry)
    assert (marked["state"], marked["placement_state"]) == ("active", "stays")


async def test_worker_with_an_invalid_answer_leaves_it_for_the_next_checkpoint(
    worker_env,
) -> None:
    worker, db, _, _, select, monkeypatch = worker_env
    entry = select(
        {**_lesson("no answer"), "rationale": "r"},
        placement={"q1": "galaxy"},
    )
    seen: list[tuple[str, str]] = []
    monkeypatch.setattr(
        "core.placement_outcomes.log_outcome",
        lambda **kwargs: seen.append((kwargs["outcome"], kwargs["reason"])),
    )

    await _drain(worker)

    assert seen == [("unplaced", "invalid-output")]
    assert (_mark(db, entry)["state"], _mark(db, entry)["placement_state"]) == (
        "active", None,
    )

@pytest.mark.parametrize("rationale", [
    "alex wants this every turn",
    "Alex's lane needs this every turn",
])
def test_commit_message_screens_the_thread_owners_name(
    tmp_path, monkeypatch, rationale,
) -> None:
    from core.placement_outcomes import _message

    db = _db(str(tmp_path / "msg.db"))
    _seed_thread(db)
    db.execute("UPDATE threads SET user_id='Alex' WHERE id='thread-1'")
    db.commit()
    monkeypatch.setattr("infra.database.get_db", lambda: _connect(str(tmp_path / "msg.db")))
    entry = {"thread_id": "thread-1", "entry_id": 1, "revision": 1,
             "body": _lesson("small commits")}

    message = _message(entry, _decision(rationale=rationale), "a/b.md", "Placement: t/1@1")

    assert "Reason: agent lesson needed every turn\n" in message
    assert "lex" not in message


def test_commit_message_keeps_a_clean_rationale(tmp_path, monkeypatch) -> None:
    from core.placement_outcomes import _message

    db = _db(str(tmp_path / "msg.db"))
    _seed_thread(db)
    db.commit()
    monkeypatch.setattr("infra.database.get_db", lambda: _connect(str(tmp_path / "msg.db")))
    entry = {"thread_id": "thread-1", "entry_id": 1, "revision": 1,
             "body": _lesson("small commits")}

    message = _message(entry, _decision(), "a/b.md", "Placement: t/1@1")

    assert "Reason: applies to this lane every turn\n" in message
