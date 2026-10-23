"""HG-1a, HG-2, HG-6, HG-7: a lesson enters through ``place`` with four stated fields.

The real tool call, worker and outcome-event table; only the thread's database
connection and workspace are pointed at disposable ones.
"""

from __future__ import annotations

import json
import sqlite3
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import pytest

from tests.project_ref_helpers import select_workspace
from tests.test_checkpoint_placement import _add, _drain, _log, _mark, _raw, _repo
from tests.test_continuity_staging import _connect, _db, _seed_thread, _tool_db

THREAD = "owner"
PERSON_WORDS = "Always cite the commit a check ran on."
FIELDS = {
    "decision": "reporting a check result as evidence",
    "rule": "Name the exact commit the check ran on before reporting it.",
    "second_case": "Quoting a figure from a spreadsheet: name the sheet revision it came from.",
    "not_when": "When the person asked for a rough guess.",
}
WRONG = "read the stamp on this frame"


@dataclass
class Env:
    workspace: Path
    tray: Path
    db: sqlite3.Connection
    continuity: Any
    worker: Any
    monkeypatch: Any


@pytest.fixture
def env(tmp_path, monkeypatch) -> Env:
    from core import placement_worker

    workspace = _repo(tmp_path)
    select_workspace(monkeypatch, workspace)
    path = tmp_path / "gate.db"
    db = _db(str(path))
    _seed_thread(db, THREAD)
    db.execute("UPDATE threads SET title='Lane', agent_enabled=1 WHERE id=?", (THREAD,))
    db.commit()
    monkeypatch.setattr("infra.database.get_db", lambda: _connect(str(path)))
    monkeypatch.setattr(
        "core.context_source_paths.append_context_source_change", lambda *a, **k: None,
    )
    return Env(
        workspace, workspace / ".threads" / THREAD, db, _tool_db(path, monkeypatch),
        placement_worker, monkeypatch,
    )


def _entry(env: Env, text: str = "citing the head avoids rework", refs: list[dict] | None = None) -> int:
    refs = refs if refs is not None else [{"kind": "user", "reference": "said so"}]
    return _add(env.db, "decisions", {"statement": text, "rationale": "r", "source_refs": refs}, THREAD)


def _place(env: Env, entry: int, verdict: str = "written", **params: Any) -> str:
    params.setdefault("placement", _raw())
    return env.continuity._run(THREAD, "place", {"entry_id": entry, "verdict": verdict, **params})


def _facts(env: Env) -> list[dict]:
    rows = env.db.execute(
        "SELECT attributes, agent_thread_id FROM outcome_events WHERE event_name='measurement.lesson_gate' ORDER BY ts, id"
    ).fetchall()
    return [{**json.loads(row["attributes"]), "thread": row["agent_thread_id"]} for row in rows]


def _state(env: Env, entry: int) -> tuple[str, str | None]:
    mark = _mark(env.db, entry, THREAD)
    return mark["state"], mark["placement_state"]


def _person_message(
    env: Env, text: str, role: str = "user", metadata: str | None = None, thread_id: str = THREAD,
) -> str:
    count = env.db.execute("SELECT count(*) FROM messages").fetchone()[0]
    message_id = f"msg-{count}"
    env.db.execute(
        "INSERT INTO messages (id,thread_id,role,content,created_at,seq,metadata) VALUES (?,?,?,?,?,?,?)",
        (message_id, thread_id, role, text, "t1", 100 + count, metadata),
    )
    env.db.commit()
    return message_id


# -- HG-2 and HG-6: four fields, a refusal that teaches ----------------------------


def test_place_without_the_fields_names_each_missing_field_and_gives_one_pair(env: Env) -> None:
    entry = _entry(env)

    reply = _place(env, entry, decision=FIELDS["decision"], rule=FIELDS["rule"])

    named = reply.splitlines()[0]
    assert named.startswith("Error:") and "second_case" in named and "not_when" in named
    assert "decision" not in named and "rule" not in named
    assert reply.count("Wrong") == 1 and reply.count("Right") == 1 and WRONG in reply
    assert _state(env, entry) == ("active", None)


def test_a_field_that_is_not_plain_text_is_named_by_its_tool_name(env: Env) -> None:
    entry = _entry(env)
    fields: dict[str, Any] = {**FIELDS, "decision": {"wrong": WRONG, "right": "a decision"}, "rule": "   "}

    reply = _place(env, entry, **fields)

    named = reply.splitlines()[0]
    assert named.startswith("Error:") and "decision" in named and "rule" in named
    assert "second_case" not in named and "not_when" not in named
    assert _state(env, entry) == ("active", None)


def test_the_refusal_defines_only_the_fields_it_names(env: Env) -> None:
    from core.lesson_gate import DEFINITIONS

    reply = _place(env, _entry(env), **{key: FIELDS[key] for key in ("decision", "rule", "not_when")})

    assert DEFINITIONS["second_case"] in reply
    for field in ("decision", "rule", "not_when"):
        assert DEFINITIONS[field] not in reply


def test_every_field_has_a_one_line_definition_in_the_tool_text(env: Env) -> None:
    from tools.continuity import ContinuityEditTool

    tool = ContinuityEditTool()
    properties = tool.parameters["properties"]

    for field in ("decision", "rule", "second_case", "not_when", "widens", "message_id"):
        description = properties[field]["description"]
        assert description and "\n" not in description, field
    assert tool.validate_params({"operation": "place", "entry_id": 1, "verdict": "proposed", "placement": _raw(), **FIELDS}) == []


def test_place_already_writes_nothing_and_needs_no_fields(env: Env) -> None:
    quote = "Keep each commit small and focused."
    skill = env.tray / "skills" / "a" / "SKILL.md"
    skill.parent.mkdir(parents=True)
    skill.write_text(f"---\nname: a\ndescription: Use when committing.\n---\n\n# A\n\n- {quote}\n")
    entry = _entry(env)

    reply = env.continuity._run(
        THREAD, "place",
        {"entry_id": entry, "verdict": "already", "destination": f".threads/{THREAD}/skills/a/SKILL.md", "quote": quote},
    )

    assert reply.startswith(f"Entry {entry} marked already at")
    assert _facts(env) == []


@pytest.mark.parametrize("verdict", ["written", "trial"])
def test_every_lesson_destination_needs_the_fields(env: Env, verdict: str) -> None:
    reply = _place(env, _entry(env), verdict, placement=_raw(q1="agent"))

    assert reply.startswith("Error:") and "decision" in reply and "not_when" in reply


def test_a_proposed_placement_is_not_forced_into_lesson_fields(env: Env) -> None:
    reply = _place(env, _entry(env), "proposed", placement=_raw(q1="project"))

    assert reply.startswith("Entry 1 queued")


# -- HG-1a: place written performs the write ---------------------------------------


async def test_place_written_queues_the_lesson_and_the_worker_writes_it(env: Env) -> None:
    entry = _entry(env)
    line = "Name the exact commit a check ran on."

    reply = _place(env, entry, placement=_raw(text=line), **FIELDS)

    assert reply.startswith(f"Entry {entry} queued")
    assert _state(env, entry) == ("active", "selected")
    bootloader = env.tray / "context" / "RESPONSIBILITY.md"
    assert not bootloader.exists()

    await _drain(env.worker)

    assert f": {line}" in bootloader.read_text() and "## Lessons" in bootloader.read_text()
    assert _state(env, entry) == ("retired", "written")
    assert _log(env.workspace)[0].startswith("placement: written")


def test_place_written_refuses_a_placement_that_is_not_an_own_destination(env: Env) -> None:
    project = _place(env, _entry(env), placement=_raw(q1="project"), **FIELDS)
    thread = _place(env, _entry(env, "other"), placement=_raw(q1="thread"), **FIELDS)

    assert project.startswith("Error:") and "proposed" in project
    assert thread.startswith("Error:") and "stays in continuity" in thread
    assert _facts(env) == []


def test_place_written_takes_a_placement_or_a_receipt_not_both(env: Env) -> None:
    params = {"verdict": "written", "entry_id": 1, "placement": _raw(), "destination": "x.md",
              "quote": "a quoted line that is long enough", **FIELDS}

    reply = env.continuity._execution_error("place", params)

    assert reply.startswith("Error:") and "not both" in reply


def test_a_receipt_with_the_fields_still_retires_on_the_quote(env: Env) -> None:
    quote = "Keep each commit small and focused."
    skill = env.tray / "skills" / "a" / "SKILL.md"
    skill.parent.mkdir(parents=True)
    skill.write_text(f"---\nname: a\ndescription: Use when committing.\n---\n\n# A\n\n- {quote}\n")
    entry = _entry(env)

    reply = env.continuity._run(
        THREAD, "place",
        {"entry_id": entry, "verdict": "written", "destination": f".threads/{THREAD}/skills/a/SKILL.md", "quote": quote, **FIELDS},
    )

    assert reply.startswith(f"Entry {entry} marked written at")
    assert _state(env, entry) == ("retired", "written")


# -- HG-1a: a person's message skips HG-2 where its destination is a lesson ----------------


def test_a_person_instruction_with_a_resolving_message_id_skips_the_fields(env: Env) -> None:
    message = _person_message(env, f"Please do this. {PERSON_WORDS} Thanks.")
    entry = _entry(env)

    reply = _place(env, entry, placement=_raw(text=PERSON_WORDS), message_id=message)

    assert reply.startswith(f"Entry {entry} queued")
    assert _facts(env)[-1]["reason"] == "verbatim"


def test_a_message_id_from_another_thread_in_the_project_resolves(env: Env) -> None:
    other = "other"
    _seed_thread(env.db, other)
    env.db.execute("INSERT INTO projects (id,name,created_at,updated_at) VALUES ('project','Project','t','t')")
    env.db.execute("UPDATE threads SET project_id='project' WHERE id IN (?, ?)", (THREAD, other))
    env.db.commit()
    message = _person_message(env, PERSON_WORDS, thread_id=other)
    from core.lesson_gate import _person_said

    assert _person_said(env.db, THREAD, message, PERSON_WORDS)


@pytest.mark.parametrize("case", ["agent-role", "other-words", "wrapper", "missing"])
def test_a_message_id_that_does_not_resolve_names_its_own_cause(env: Env, case: str) -> None:
    refs = {
        "agent-role": _person_message(env, PERSON_WORDS, role="assistant"),
        "other-words": _person_message(env, "Something else entirely, said at length."),
        "wrapper": _person_message(env, f"[PASSIVE CONTEXT DIGEST] {PERSON_WORDS}"),
        "missing": "no-such-message",
    }

    reply = _place(env, _entry(env), placement=_raw(text=PERSON_WORDS), message_id=refs[case])

    assert reply.startswith("Error:") and "decision" in reply and "message_id" in reply
    assert "no person-written message in this project contains the line" in reply


# -- widens: optional, existence only -------------------------------------------------


def test_widens_names_an_earlier_lesson_and_is_recorded(env: Env) -> None:
    earlier = _entry(env, "earlier")
    entry = _entry(env)

    reply = _place(env, entry, widens=earlier, **FIELDS)

    assert reply.startswith(f"Entry {entry} queued")
    assert _facts(env)[-1]["widens"] == str(earlier)


@pytest.mark.parametrize("widens", [999, "own"])
def test_widens_must_name_another_existing_entry(env: Env, widens) -> None:
    entry = _entry(env)

    reply = _place(env, entry, widens=entry if widens == "own" else widens, **FIELDS)

    assert reply.startswith("Error:") and "widens" in reply
    assert _facts(env)[-1]["result"] == "refused" and _facts(env)[-1]["reason"] == "widens"
    assert _state(env, entry) == ("active", None)


# -- HG-7: every refusal and every widens link is a fact ------------------------------


def test_a_refusal_is_recorded_as_one_outcome_fact(env: Env) -> None:
    _place(env, _entry(env), decision=FIELDS["decision"])

    assert _facts(env) == [{
        "result": "refused", "reason": "fields", "verdict": "written",
        "fields": "rule,second_case,not_when", "thread": THREAD,
    }]


def test_an_accepted_lesson_is_recorded_with_its_decision(env: Env) -> None:
    _place(env, _entry(env), **FIELDS)

    assert _facts(env) == [{
        "result": "accepted", "reason": "fields", "verdict": "written",
        "decision": FIELDS["decision"], "thread": THREAD,
    }]


def test_a_count_neutral_save_reword_is_recorded(env: Env) -> None:
    from infra.guidance_git import save_guidance
    from infra.guidance_targets import GuidanceTarget

    path = env.tray / "context" / "RESPONSIBILITY.md"
    path.parent.mkdir(parents=True)
    path.write_text("# Role\n\n## Lessons\n\n- Keep commits focused.\n")

    saved = save_guidance(
        {path: b"# Role\n\n## Lessons\n\n- Keep commits small and focused.\n"},
        workspace=env.workspace, target=GuidanceTarget("agent"),
        author=f"Lane <{THREAD}@agents.corsia.local>",
    )

    assert saved.outcome == "written"
    assert _facts(env) == [{"result": "accepted", "reason": "rewording", "verdict": "save", "thread": THREAD}]


def test_a_file_edit_refusal_is_recorded_and_the_learning_report_counts_it(env: Env) -> None:
    from core.lesson_gate import gate_counts
    from infra.guidance_git import save_guidance
    from infra.guidance_targets import GuidanceTarget

    path = env.tray / "context" / "RESPONSIBILITY.md"
    saved = save_guidance(
        {path: b"# Role\n\n## Lessons\n\n- 2026-10-23: read the stamp on this frame.\n"},
        workspace=env.workspace, target=GuidanceTarget("agent"),
        author=f"Lane <{THREAD}@agents.corsia.local>",
    )
    _place(env, _entry(env), decision="only one")
    _place(env, _entry(env, "again"), widens=None, **FIELDS)

    assert (saved.outcome, saved.reason) == ("refused", "lesson-outside-placement")
    assert not path.exists()
    assert _facts(env)[0] == {"result": "refused", "reason": "lesson-outside-placement", "verdict": "save", "thread": THREAD}
    assert gate_counts(env.db, (THREAD,), "0000", "9999") == {"refused": 2, "accepted": 1, "widens": 0}
