"""Placing a hypothesis as a trial: the real tool call, worker, review and installed offer.

Nothing between the ``place`` call and the installed skills index is replaced
here: the thread's tray, the versioned writer, the artifact registry, the Plans
review and the skills registry are the real ones. A fault is one real step
raising once.

- A complete hypothesis is written, reviewed and offered by the next install,
  and its continuity entry retires. Until the review reference is written, no
  index offers the file.
- A failure at any step leaves the trial out of the index and its entry
  recoverable; the retry repeats nothing: one file, one artifact, one review.
- A person's instruction is never a trial. A hypothesis resting on a person's
  words without a derivation, or without their words kept somewhere, is
  captured: a file and a review, not guidance. Their words stay where they are.
- Appended evidence finds the same captured review; completing the capture
  closes it and opens the trial's.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
import sqlite3
from typing import Any

import pytest

from core import lesson_files
from core.skills_registry import skill_context_sources
from tests.project_ref_helpers import select_workspace
from tests.test_checkpoint_placement import _add, _drain, _log, _mark, _raw, _repo
from tests.test_continuity_staging import _connect, _db, _seed_thread, _tool_db

THREAD = "owner"
PROVENANCE = "lesson.trial_review"
TURN = [{"kind": "turn", "reference": "turn-7"}]
SAID = [{"kind": "user", "reference": "message 41"}]


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
    monkeypatch.setattr("tools.sandbox_config._STATIC_ALLOWED_RW", [workspace.resolve()])
    path = tmp_path / "lessons.db"
    db = _db(str(path))
    _seed_thread(db, THREAD)
    db.execute("UPDATE threads SET title='Lane', agent_enabled=1 WHERE id=?", (THREAD,))
    db.commit()
    monkeypatch.setattr("infra.database.get_db", lambda: _connect(str(path)))
    return Env(
        workspace, workspace / ".threads" / THREAD, db, _tool_db(path, monkeypatch),
        placement_worker, monkeypatch,
    )


def _due(**delta) -> str:
    # A date, not an instant: compute it once per test and reuse it, or a run
    # that crosses midnight UTC compares two different days.
    return (datetime.now(timezone.utc) + timedelta(**delta)).date().isoformat()


def _answer(*, trial: dict | None = None, **overrides) -> dict:
    answer = _raw(
        q2="task_start", text="Name the exact commit a check ran on.",
        skill_name="cite-the-head",
        skill_description="Use when reporting a check result on a pull request.",
        rationale="a hypothesis to try in this lane",
    )
    answer["trial"] = {
        "kind": "hypothesis", "review_after": _due(days=14), "boundary": "A one-line typo fix.",
        **(trial or {}),
    }
    answer.update(overrides)
    return answer


def _entry(env: Env, refs: list[dict] = TURN, text: str = "citing the head avoids rework") -> int:
    return _add(env.db, "decisions", {"statement": text, "rationale": "r", "source_refs": refs}, THREAD)


GATE_FIELDS = {
    "decision": "reporting a check result as evidence",
    "rule": "Name the exact commit the check ran on before reporting it.",
    "second_case": "Quoting a figure from a spreadsheet: name the sheet revision it came from.",
    "not_when": "When the person asked for a rough guess.",
}


def _place(env: Env, entry: int, answer: dict | None = None) -> str:
    return env.continuity._run(
        THREAD, "place",
        {"entry_id": entry, "verdict": "trial", "placement": answer or _answer(), **GATE_FIELDS},
    )


def test_trial_with_existing_target_section_is_refused_at_admission(env: Env) -> None:
    target = env.workspace / "context" / "rule.md"
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text("# Rules\n\n## Lessons\nExisting.\n")
    result = _place(env, _entry(env), _answer(
        case=CASE, cause="rule_gap", target_file="context/rule.md", target_section="Lessons",
    ))
    assert "already in context" in result
    assert "may only add a new section" in result
    assert not _skill(env).exists()


def test_flagged_new_checkpoint_entry_records_an_overridden_trial_check(env: Env) -> None:
    """A checkpoint-local entry has no revision yet, but the check remains non-blocking."""
    from types import SimpleNamespace

    from core import lesson_admission, placement_answer, placement_outcomes
    from core.placement_decide import validate_decision

    env.monkeypatch.setattr(lesson_admission, "admit", lambda *_args: SimpleNamespace(hold=None))
    env.monkeypatch.setattr(placement_outcomes, "entry_text", lambda _entry: "source")
    decision = validate_decision(_answer(
        text="On 2026-10-15, run the check.",
        trial={"incident_detail_reason": "needed for the test"},
    ))
    result = placement_answer._stage_trial(
        env.db, THREAD, {"history_epoch": 0, "entry_id": 999, "revision": None},
        SimpleNamespace(), decision, SimpleNamespace(), None,
    )

    assert result["placement_check"]["flags"] == ["date", "id"]
    row = env.db.execute(
        "SELECT revision, override_reason FROM lesson_placement_checks WHERE entry_id=999"
    ).fetchone()
    assert tuple(row) == (0, "needed for the test")


def _state(env: Env, entry: int) -> tuple[str, str | None]:
    mark = _mark(env.db, entry, THREAD)
    return mark["state"], mark["placement_state"]


def _skill(env: Env, name: str = "cite-the-head") -> Path:
    return env.tray / "skills" / name / "SKILL.md"


def _offered(env: Env) -> list[dict]:
    """Eligible lessons leave cached context and can reach a task's with arm."""
    from core.lesson_delivery import draw, start_trials
    from core.lesson_index import partition
    from core.skill_resolution import resolve_effective_skills

    sources = skill_context_sources(thread_id=THREAD, project_id=None, workspace=env.workspace, db=env.db)
    index = next(source for source in sources if source.source_id == "dynamic:skills-index")
    assert index.metadata["lessons"] == []
    resolved = resolve_effective_skills(thread_id=THREAD, project_id=None, workspace=env.workspace, db=env.db)
    split = partition(resolved.included, resolved.omitted, thread_id=THREAD, db=env.db)
    records = []
    for record in split.lessons:
        assert record["index_line"] not in index.prompt_content
        node = next(f"verify-{record['id']}-{n}" for n in range(1000)
                    if draw(record["id"], record["behavioral_sha256"], f"verify-{record['id']}-{n}") == "with")
        delivered = start_trials(env.db, thread_id=THREAD, node_id=node)
        if any(item["lesson_id"] == record["id"] for item in delivered):
            records.append(record)
    return records


def _reviews(env: Env, prefix: str = "") -> list[sqlite3.Row]:
    return env.db.execute(
        """SELECT n.id, n.state, n.title, l.source_event_id AS key
           FROM plan_node_artifact_links l JOIN plan_nodes n ON n.id=l.node_id
           WHERE l.provenance=? AND l.source_event_id LIKE ? ORDER BY n.created_at, n.id""",
        (PROVENANCE, f"{prefix}%"),
    ).fetchall()


def _open(rows: list[sqlite3.Row]) -> list[sqlite3.Row]:
    return [row for row in rows if row["state"] == "pending"]


def _offered_at(env: Env, now: datetime) -> list[dict]:
    """The lessons an index installed at ``now`` would carry."""
    from core import lesson_index
    from core.skill_resolution import resolve_effective_skills

    resolved = resolve_effective_skills(
        thread_id=THREAD, project_id=None, workspace=env.workspace, db=env.db,
    )
    return lesson_index.partition(
        resolved.included, resolved.omitted, thread_id=THREAD, db=env.db, now=now,
    ).lessons


def _row(env: Env, entry: int) -> dict:
    return dict(env.db.execute(
        "SELECT * FROM continuity_entries WHERE thread_id=? AND entry_id=?", (THREAD, entry),
    ).fetchone())


def _artifacts(env: Env, path: Path) -> int:
    return env.db.execute(
        "SELECT COUNT(*) FROM artifact_identities WHERE path=?", (str(path.resolve()),),
    ).fetchone()[0]


def _once(env: Env, target: str, *, skip: int = 0, result: Any = None) -> None:
    """Make one real step fail once, after ``skip`` successful calls."""
    module, name = target.rsplit(".", 1)
    import importlib

    owner = importlib.import_module(module)
    real = getattr(owner, name)
    calls = {"count": 0}

    def flaky(*args, **kwargs):
        calls["count"] += 1
        if calls["count"] == skip + 1:
            if result is not None:
                return result
            raise RuntimeError(f"injected failure in {name}")
        return real(*args, **kwargs)

    env.monkeypatch.setattr(owner, name, flaky)


# -- activation ----------------------------------------------------------------


async def test_a_hypothesis_becomes_a_reviewed_trial_the_next_install_offers(env: Env) -> None:
    entry = _entry(env)
    due = _due(days=14)

    reply = _place(env, entry, _answer(trial={"review_after": due}))

    assert reply == (
        f"Entry {entry} queued: CORSIA writes the trial skill cite-the-head, opens your "
        f"review task for {due} and retires the entry. It is not in this "
        "conversation's skills list; it appears when the list is next rebuilt."
    )
    assert not _skill(env).exists() and _offered(env) == []

    await _drain(env.worker)

    trial = lesson_files.read_trial(_skill(env).read_text())
    assert trial is not None and trial.lesson.owner == THREAD
    assert trial.lesson.source_refs == tuple(TURN)
    [review] = _reviews(env)
    assert (review["state"], review["title"]) == ("pending", "Review trial lesson: cite-the-head")
    assert review["key"] == f"lesson:{trial.lesson.id}:{trial.behavioral_sha256}"
    assert trial.lesson.review_ref == f"plan-node:{review['id']}"
    description = env.db.execute("SELECT description FROM plan_nodes WHERE id=?", (review["id"],)).fetchone()[0]
    assert f'lesson_report(lesson_id="{trial.lesson.id}", version="{trial.behavioral_sha256}")' in description
    assert "not application or benefit" in description
    [watch] = env.db.execute(
        "SELECT condition_type FROM expected_event_watches WHERE plan_node_id=? AND status='armed'",
        (review["id"],),
    ).fetchall()
    assert watch["condition_type"] == "deadline"
    [offered] = _offered(env)
    assert (offered["id"], offered["behavioral_sha256"], offered["name"]) == (
        trial.lesson.id, trial.behavioral_sha256, "cite-the-head",
    )
    assert _state(env, entry) == ("retired", "written")
    assert _artifacts(env, _skill(env)) == 1
    assert _log(env.workspace)[:2] == [
        "placement: trial offered .threads/owner/skills/cite-the-head/SKILL.md",
        "placement: trial written .threads/owner/skills/cite-the-head/SKILL.md",
    ]
    assert "Name the exact commit" not in "\n".join(_log(env.workspace))


FAULTS = {
    "the file write": ("infra.guidance_git.save_guidance", 0, False, 0),
    "the artifact registration": ("registry.service.register_write_strict", 0, True, 0),
    "the artifact link": ("plans.artifact_links.link", 0, True, 0),
    "the schedule": ("plans.scheduled_wait.schedule_task", 0, True, 0),
    "the review reference write": ("infra.guidance_git.save_guidance", 1, True, 1),
}


@pytest.mark.parametrize("fault", sorted(FAULTS))
async def test_a_failed_step_offers_nothing_and_the_retry_repeats_nothing(env: Env, fault) -> None:
    target, skip, written, reviews = FAULTS[fault]
    entry = _entry(env)
    _place(env, entry)
    _once(env, target, skip=skip)

    await _drain(env.worker)

    assert _skill(env).exists() is written
    assert _offered(env) == []
    assert len(_reviews(env)) == reviews  # a failed review write leaves no node behind
    assert _state(env, entry) == ("active", None)

    _place(env, entry)
    await _drain(env.worker)

    [review] = _reviews(env)
    [offered] = _offered(env)
    assert review["state"] == "pending"  # never started: it waits for its date
    assert lesson_files.read_trial(_skill(env).read_text()).lesson.review_ref == (
        f"plan-node:{review['id']}"
    )
    assert offered["name"] == "cite-the-head"
    assert _artifacts(env, _skill(env)) == 1
    assert _state(env, entry) == ("retired", "written")


async def test_a_file_edited_under_the_writer_is_not_overwritten_blind(env: Env) -> None:
    from registry import service

    entry = _entry(env)
    _place(env, entry)
    real = service.register_write_strict

    def edited_meanwhile(thread_id, path, **kwargs):
        with open(path, "a", encoding="utf-8") as handle:
            handle.write("\nSomeone else's line.\n")
        return real(thread_id, path, **kwargs)

    env.monkeypatch.setattr(service, "register_write_strict", edited_meanwhile)

    await _drain(env.worker)

    assert "Someone else's line." in _skill(env).read_text()
    assert _offered(env) == []
    assert _state(env, entry) == ("active", "selected")  # transient: the next wake retries


async def test_an_owner_who_cannot_hold_a_review_gets_no_offered_trial(env: Env) -> None:
    from plans import service

    plan = service.ensure_plan("thread", THREAD, db=env.db)
    env.db.execute("UPDATE plans SET state='archived' WHERE id=?", (plan.id,))
    env.db.commit()
    entry = _entry(env)
    _place(env, entry)

    await _drain(env.worker)

    assert lesson_files.read_trial(_skill(env).read_text()).lesson.review_ref is None
    assert _offered(env) == [] and _reviews(env) == []
    assert _state(env, entry) == ("active", None)


async def test_a_revision_keeps_the_lesson_and_replaces_its_review(env: Env) -> None:
    first = _entry(env)
    _place(env, first)
    await _drain(env.worker)
    before = lesson_files.read_trial(_skill(env).read_text())
    second = _entry(env, [{"kind": "turn", "reference": "turn-19"}], "also name the branch")

    _place(env, second, _answer(text="Name the exact commit and branch a check ran on."))
    await _drain(env.worker)

    after = lesson_files.read_trial(_skill(env).read_text())
    assert after.lesson.id == before.lesson.id
    assert after.behavioral_sha256 != before.behavioral_sha256
    assert [ref["reference"] for ref in after.lesson.source_refs] == ["turn-7", "turn-19"]
    old, new = _reviews(env)
    assert (old["state"], new["state"]) == ("closed", "pending")
    assert [lesson["behavioral_sha256"] for lesson in _offered(env)] == [after.behavioral_sha256]


CASE = {
    "what_happened": "The placement case was not saved.",
    "right_call": "Persist the case through the existing lesson writer.",
    "does_not_apply": "A lesson with no case.",
    "actions": {
        "required_actions": [
            {"tool": "read_file", "params": {"path": "context/rule.md"}},
            {"tool": "plan_update", "params": {"operation": "mark_done"}},
        ],
        "forbidden_actions": [{"tool": "write_file", "params": {"path": "context/rule.md"}}],
        "decisive_action": {"tool": "plan_update", "params": {"operation": "mark_done"}},
    },
}


async def test_a_trial_keeps_its_case_and_a_revision_replaces_only_what_it_supplies(env: Env) -> None:
    target = env.workspace / "context" / "rule.md"
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text("# Rules\n")
    first = _entry(env)
    _place(env, first, _answer(
        case=CASE, cause="rule_gap", target_file="context/rule.md", target_section="Lessons",
    ))
    await _drain(env.worker)
    before = lesson_files.read_trial(_skill(env).read_text())
    assert before is not None
    assert before.lesson.case_metadata == {
        "target_file": "context/rule.md", "target_section": "Lessons", "cause": "rule_gap", "case": CASE,
    }

    second = _entry(env, [{"kind": "turn", "reference": "turn-19"}], "also name the branch")
    revised = _place(env, second, _answer(
        text="Name the exact commit and branch a check ran on.",
        target_section="Checks", new_evidence="target_changed",
    ))
    await _drain(env.worker)

    assert not revised.startswith("Error")

    after = lesson_files.read_trial(_skill(env).read_text())
    assert after is not None and after.lesson.id == before.lesson.id
    assert after.lesson.case_metadata == {
        **before.lesson.case_metadata, "target_section": "Checks", "new_evidence": "target_changed",
    }


async def test_a_revision_never_keeps_stale_evidence_or_an_old_target(env: Env) -> None:
    target = env.workspace / "context" / "rule.md"
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text("# Rules\n")
    _place(env, _entry(env), _answer(case=CASE, target_file="context/rule.md", target_section="Lessons"))
    await _drain(env.worker)
    _place(env, _entry(env, [{"kind": "turn", "reference": "turn-19"}], "a new file"), _answer(
        text="Name the exact commit and branch a check ran on.",
        new_target_path="context/checks.md", new_evidence="target_changed",
    ))
    await _drain(env.worker)
    retargeted = lesson_files.read_trial(_skill(env).read_text())
    assert retargeted.lesson.case_metadata == {
        "new_target_path": "context/checks.md", "case": CASE, "new_evidence": "target_changed",
    }

    _place(env, _entry(env, [{"kind": "turn", "reference": "turn-23"}], "and the repo"), _answer(
        text="Name the exact repo, commit and branch a check ran on.",
    ))
    await _drain(env.worker)

    after = lesson_files.read_trial(_skill(env).read_text())
    assert after.lesson.id == retargeted.lesson.id
    assert after.lesson.case_metadata == {"new_target_path": "context/checks.md", "case": CASE}


async def test_a_held_revision_offered_later_keeps_only_the_new_evidence_it_states(env: Env) -> None:
    _place(env, _entry(env), _answer(case=CASE))
    await _drain(env.worker)
    entry = _entry(env, [{"kind": "turn", "reference": "turn-19"}], "also name the branch")
    revision = _answer(text="Name the exact commit and branch a check ran on.", target_section="Checks")

    _place(env, entry, {**revision, "trial": {**revision["trial"], "boundary": None},
                        "new_evidence": "case_differs"})
    await _drain(env.worker)
    [capture] = (env.tray / "context" / "captures").glob("*.md")
    assert lesson_files.read_capture(capture.read_text()).case_metadata == {
        "target_section": "Checks", "new_evidence": "case_differs",
    }

    _place(env, entry, revision)
    await _drain(env.worker)

    after = lesson_files.read_trial(_skill(env).read_text())
    assert after.statement == "Name the exact commit and branch a check ran on."
    assert after.lesson.case_metadata == {"case": CASE, "target_section": "Checks"}


async def test_a_held_capture_keeps_its_case_and_its_offer_carries_it(env: Env) -> None:
    target = env.workspace / "context" / "rule.md"
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text("# Rules\n")
    literal = _entry(env, SAID, "the user said: that answer was wrong")
    entry = _entry(env, SAID, "always search the history first")
    case: dict[str, Any] = {"case": CASE, "cause": "rule_gap", "target_file": "context/rule.md"}

    held = _place(env, entry, _answer(**case))
    await _drain(env.worker)

    assert held.startswith(f"Entry {entry} queued as a captured hypothesis")
    [capture] = (env.tray / "context" / "captures").glob("*.md")
    assert lesson_files.read_capture(capture.read_text()).case_metadata == case

    _place(env, entry, _answer(trial={
        "derivation": "they rejected an answer that skipped the earlier decision",
        "literal_ref": {"entry_id": literal},
    }, target_section="Lessons"))
    await _drain(env.worker)

    trial = lesson_files.read_trial(_skill(env).read_text())
    assert trial.lesson.case_metadata == {**case, "target_section": "Lessons"}


async def test_a_held_answer_gives_new_evidence_only_when_it_revises(env: Env) -> None:
    entry = _entry(env, SAID, "always search the history first")

    reply = _place(env, entry, _answer(new_evidence="case_differs"))
    await _drain(env.worker)

    assert reply.startswith("Error: new_evidence is only for revising your own watch or trial lesson")
    assert _state(env, entry) == ("active", None)
    assert list((env.tray / "context" / "captures").glob("*.md")) == []


async def test_a_retired_lesson_tried_again_is_a_new_lesson_naming_its_origin(env: Env) -> None:
    first = _entry(env)
    _place(env, first)
    await _drain(env.worker)
    old = lesson_files.read_trial(_skill(env).read_text())
    _skill(env).write_text(_skill(env).read_text().replace("state: trial", "state: retired"))
    assert _offered(env) == []
    second = _entry(env, [{"kind": "turn", "reference": "turn-40"}], "citing the head still helps")

    _place(env, second)
    await _drain(env.worker)

    new = lesson_files.read_trial(_skill(env).read_text())
    assert new.lesson.state == "trial" and new.lesson.id != old.lesson.id
    assert new.lesson.origin == {"id": old.lesson.id, "behavioral_sha256": old.behavioral_sha256}
    assert [ref["reference"] for ref in new.lesson.source_refs] == ["turn-40"]
    [review] = _open(_reviews(env, f"lesson:{new.lesson.id}:"))
    assert new.lesson.review_ref == f"plan-node:{review['id']}"
    assert [lesson["id"] for lesson in _offered(env)] == [new.lesson.id]


async def test_a_checkpoint_stages_a_trial_and_its_install_hands_it_to_the_worker(
    env: Env,
) -> None:
    import infra.database as database
    from infra import checkpoint_install_preflight, checkpoint_queries
    from infra.checkpoints import create_running
    from tests.test_continuity_staging import _capture_install, _request

    assert checkpoint_install_preflight.get_db is not database.get_db
    assert checkpoint_queries.get_db is not database.get_db

    complete = _entry(env)
    held = _entry(env, SAID, "always search the history first")
    create_running(_request(THREAD, "cp-1"), db=env.db)
    env.db.commit()

    def review(entry: int, answer: dict) -> str:
        return env.continuity._run(
            THREAD, "review", {"entry_id": entry, "verdict": "select", "placement": answer},
            "cp-1",
        )

    refused = review(complete, _answer(trial={"kind": "instruction"}))
    staged = review(complete, _answer())
    captured = review(held, _answer(skill_name="search-first"))

    assert refused.startswith("Error: a trial is only for a hypothesis")
    assert staged == f"Staged entry {complete} verdict select."
    assert captured.startswith(
        f"Staged entry {held} verdict select as a captured hypothesis, not offered as guidance: "
    )
    assert captured.endswith(f"then call place with verdict trial for entry {held}.")
    await _drain(env.worker)
    assert not _skill(env).exists() and _reviews(env) == []  # nothing before install

    _capture_install(env.db)
    env.db.commit()
    await _drain(env.worker)

    assert [lesson["name"] for lesson in _offered(env)] == ["cite-the-head"]
    assert _state(env, complete) == ("retired", "written")
    assert _state(env, held) == ("active", "stays")
    assert len(_open(_reviews(env, "capture:"))) == 1


# -- refusals --------------------------------------------------------------------


@pytest.mark.parametrize(("answer", "message"), [
    (_answer(trial={"kind": "instruction"}),
     "Error: a trial is only for a hypothesis. A person's instruction keeps its permanent home"),
    (_answer(q1="project"), "Error: a trial is your own hypothesis about how you work"),
    (_answer(trial={"review_after": "2020-01-01"}),
     "Error: trial.review_after must be a future date"),
    (_answer(skill_name="Not A Slug"), "Error: skill_name must be a new lowercase-hyphenated"),
    (_answer(duplicate_of="- some line"), "Error: duplicate_of and trial do not go together"),
    (_answer(trial={"boundary": "word " * 160}), "Error: a lesson body is at most 150 words"),
    (_answer(new_evidence="case_differs"),
     "Error: new_evidence is only for revising your own watch or trial lesson"),
])
async def test_an_answer_that_cannot_be_a_trial_goes_back_to_be_corrected(
    env: Env, answer, message,
) -> None:
    entry = _entry(env)

    reply = _place(env, entry, answer)
    await _drain(env.worker)

    assert reply.startswith(message)
    assert _state(env, entry) == ("active", None)
    assert not _skill(env).exists() and _reviews(env) == []


def test_a_trial_never_takes_the_name_of_another_skill(env: Env) -> None:
    taken = env.workspace / "skills" / "cite-the-head" / "SKILL.md"
    taken.parent.mkdir(parents=True)
    taken.write_text("---\nname: cite-the-head\ndescription: Use when citing.\n---\n\n# C\n")

    reply = _place(env, _entry(env))

    assert reply.startswith("Error: skill_name cite-the-head is already a skill at")


def test_a_trial_never_takes_the_name_of_a_project_skill(env: Env) -> None:
    from tests.project_ref_helpers import project_ref, write_project_context

    project = write_project_context(env.workspace, "p")
    env.db.execute(
        "INSERT INTO projects (id,name,path,created_at,updated_at) VALUES ('p','P',?,'t','t')",
        (project_ref("p"),),
    )
    env.db.execute("UPDATE threads SET project_id='p' WHERE id=?", (THREAD,))
    env.db.commit()
    taken = project / "skills" / "cite-the-head" / "SKILL.md"
    taken.parent.mkdir(parents=True)
    taken.write_text("---\nname: cite-the-head\ndescription: Use when citing.\n---\n\n# C\n")

    reply = _place(env, _entry(env))

    assert reply.startswith(f"Error: skill_name cite-the-head is already a skill at {taken}")
    assert not list(env.workspace.rglob(".threads/owner/skills/cite-the-head/SKILL.md"))


def _thread_project(env: Env) -> str | None:
    return env.db.execute("SELECT project_id FROM threads WHERE id=?", (THREAD,)).fetchone()[0]


def test_a_trial_never_takes_the_name_of_a_system_skill(env: Env) -> None:
    from core.skill_resolution import resolve_effective_skills

    seen = resolve_effective_skills(
        thread_id=THREAD, project_id=_thread_project(env), workspace=env.workspace, db=env.db,
    )
    system = [row for row in seen.included if row.scope == "system"]
    assert system, "the packaged System skills are resolved for this thread"

    reply = _place(env, _entry(env), _answer(skill_name=system[0].name))

    assert reply.startswith(f"Error: skill_name {system[0].name} is already a skill at")


def test_a_persistent_agent_with_a_spawner_keeps_trial_lessons(env: Env) -> None:
    _seed_thread(env.db, "parent")
    env.db.execute("UPDATE threads SET spawned_by='parent' WHERE id=?", (THREAD,))
    env.db.commit()

    reply = _place(env, _entry(env))

    assert reply.startswith("Entry 1 queued: CORSIA writes the trial skill cite-the-head")


def test_a_temporary_worker_keeps_no_trial_lessons(env: Env) -> None:
    _seed_thread(env.db, "parent")
    env.db.execute(
        "UPDATE threads SET spawned_by='parent', agent_enabled=0 WHERE id=?", (THREAD,),
    )
    env.db.commit()

    reply = _place(env, _entry(env))

    assert reply.startswith("Error: a temporary worker keeps no trial lessons")
    assert not _skill(env).exists() and _reviews(env) == []


def test_a_hand_written_trial_is_not_a_receipt(env: Env) -> None:
    from core.lesson_trials import Trial

    _skill(env).parent.mkdir(parents=True)
    _skill(env).write_text(lesson_files.render_trial(
        lesson_id="0f3a6f0e-7f0e-5a55-9d40-6f1f0e6f7a11", owner=THREAD, name="cite-the-head",
        description="Use when reporting a check result.", text="Name the exact commit.",
        trial=Trial(kind="hypothesis", review_after=_due(days=14)), source_refs=TURN,
    ))
    entry = _entry(env)

    reply = env.continuity._run(THREAD, "place", {
        "entry_id": entry, "verdict": "written", **GATE_FIELDS,
        "destination": ".threads/owner/skills/cite-the-head/SKILL.md",
        "quote": "Name the exact commit.",
    })

    assert reply == (
        "Error: .threads/owner/skills/cite-the-head/SKILL.md is a trial lesson not "
        "offered: trial has no scheduled review. A trial lesson is offered only once "
        "its review exists: keep the entry and place it with verdict trial, which "
        "writes the file and opens the review"
    )
    assert _state(env, entry) == ("active", None)


# -- captured, not offered -------------------------------------------------------


async def test_a_rule_inferred_from_a_persons_words_is_captured_until_it_is_derived(
    env: Env,
) -> None:
    literal = _entry(env, SAID, "the user said: that answer was wrong")
    entry = _entry(env, SAID, "always search the history first")

    reply = _place(env, entry)
    await _drain(env.worker)

    assert reply == (
        f"Entry {entry} queued as a captured hypothesis, not offered as guidance: it cites "
        "what a person said and gives no derivation, so the rule is not yet shown to follow "
        "from their words. CORSIA keeps it in a file with a review task and the entry stays. "
        "To offer it, add trial.derivation saying how the rule follows from the person's "
        f"words, then call place with verdict trial for entry {entry}."
    )
    assert not _skill(env).exists() and _offered(env) == []
    [capture] = (env.tray / "context" / "captures").glob("*.md")
    assert "Not offered as guidance" in capture.read_text()
    [review] = _reviews(env)
    assert review["state"] == "pending" and review["key"].startswith("capture:")
    assert _state(env, entry) == ("active", "stays")
    assert _state(env, literal) == ("active", None)
    assert _delivered_context(env) == []

    derived = _answer(trial={
        "derivation": "they rejected an answer that skipped the earlier decision",
        "literal_ref": {"entry_id": literal},
    })
    completed = _place(env, entry, derived)
    await _drain(env.worker)

    assert completed.startswith(f"Entry {entry} queued: CORSIA writes the trial skill")
    trial = lesson_files.read_trial(_skill(env).read_text())
    assert capture.stem == trial.lesson.id
    assert f"lesson: {trial.lesson.id}" in capture.read_text()
    assert "state: superseded" in capture.read_text()
    old, new = _reviews(env)
    assert (old["state"], new["state"]) == ("closed", "pending")
    assert new["key"].startswith("lesson:")
    assert [lesson["id"] for lesson in _offered(env)] == [trial.lesson.id]
    assert _state(env, entry) == ("retired", "written")
    assert _state(env, literal) == ("active", None)  # the person's words never expire


async def test_a_capture_keeps_correction_subjects_when_revised_and_promoted(env: Env) -> None:
    def start(turn_id: str, message_id: str) -> None:
        input_order = int(env.db.execute(
            "SELECT COALESCE(max(input_order), 0) + 1 FROM messages WHERE thread_id=?", (THREAD,),
        ).fetchone()[0])
        env.db.execute(
            """INSERT INTO active_turns(
                id,thread_id,execution_epoch,model,status,started_at,boot_epoch,usage_receipts_json
            ) VALUES (?,? ,0,'test/model','running','now','boot','{}')""", (turn_id, THREAD),
        )
        env.db.execute(
            """INSERT INTO messages(
                id,thread_id,role,content,created_at,input_order,active_turn_id
            ) VALUES (?,?,'user','Please correct this.','now',?,?)""",
            (message_id, THREAD, input_order, turn_id),
        )
        env.db.commit()

    literal = _entry(env, SAID, "the user said: that answer was wrong")
    entry = _entry(env, SAID, "always search the history first")
    start("turn-one", "first")

    _place(env, entry)
    await _drain(env.worker)
    [capture_path] = (env.tray / "context" / "captures").glob("*.md")
    assert lesson_files.read_capture(capture_path.read_text()).originating_subjects == ("message:first",)

    env.db.execute("UPDATE active_turns SET status='completed' WHERE id='turn-one'")
    start("turn-two", "second")
    _place(env, entry)
    await _drain(env.worker)
    assert lesson_files.read_capture(capture_path.read_text()).originating_subjects == (
        "message:first", "message:second",
    )

    env.db.execute("UPDATE active_turns SET status='completed' WHERE id='turn-two'")
    start("turn-three", "third")
    _place(env, entry, _answer(trial={
        "derivation": "they rejected an answer that skipped the earlier decision",
        "literal_ref": {"entry_id": literal},
    }))
    await _drain(env.worker)

    assert lesson_files.read_trial(_skill(env).read_text()).lesson.originating_subjects == (
        "message:first", "message:second", "message:third",
    )


async def test_an_expired_trial_is_no_longer_offered_and_the_persons_words_stay(
    env: Env,
) -> None:
    literal = _entry(env, SAID, "the user said: that answer was wrong")
    entry = _entry(env, SAID, "always search the history first")
    _place(env, entry, _answer(trial={
        "derivation": "they rejected an answer that skipped the earlier decision",
        "literal_ref": {"entry_id": literal},
    }))
    await _drain(env.worker)
    said, written = _row(env, literal), _skill(env).read_bytes()
    assert len(_offered(env)) == 1

    later = _offered_at(env, datetime.now(timezone.utc) + timedelta(days=15))

    assert later == []
    assert _skill(env).read_bytes() == written
    assert _row(env, literal) == said and said["state"] == "active"


def _delivered_context(env: Env) -> list[str]:
    """Thread bootloader files a turn would load: a capture must never be one."""
    from core.dynamic_context_registry import _thread_context_dir_entries

    return _thread_context_dir_entries(THREAD, None, env.workspace, env.db)


async def test_a_literal_that_is_kept_nowhere_holds_the_hypothesis(env: Env) -> None:
    entry = _entry(env, SAID)
    answer = _answer(trial={"derivation": "it follows", "literal_ref": {"entry_id": entry}})

    reply = _place(env, entry, answer)

    assert "does not point at where their exact words are kept" in reply
    assert "name that in trial.literal_ref" in reply


def _derived(**literal_ref) -> dict:
    return _answer(trial={"derivation": "it follows", "literal_ref": literal_ref})


HELD = "does not point at where their exact words are kept"
QUEUED = "queued: CORSIA writes the trial skill"


async def test_another_entry_keeps_the_words_only_if_it_cites_the_same_thing_said(
    env: Env,
) -> None:
    unrelated = _entry(env, TURN, "an unrelated note")
    elsewhere = _entry(env, [{"kind": "user", "reference": "message 99"}], "the user said: no")
    literal = _entry(env, SAID, "the user said: that answer was wrong")
    entry = _entry(env, SAID, "always search the history first")

    assert HELD in _place(env, entry, _derived(entry_id=unrelated))
    assert HELD in _place(env, entry, _derived(entry_id=elsewhere))
    assert HELD in _place(env, entry, _derived(entry_id=9999))
    assert QUEUED in _place(env, entry, _derived(entry_id=literal))


async def test_another_hypothesis_is_not_where_a_persons_words_are_kept(env: Env) -> None:
    captured = _entry(env, SAID, "prefer the short answer")
    _place(env, captured, _answer(skill_name="short-answers"))
    await _drain(env.worker)
    assert _state(env, captured) == ("active", "stays")
    queued = _entry(env, SAID, "prefer the long answer")
    literal = _entry(env, SAID, "the user said: that answer was wrong")
    assert QUEUED in _place(env, queued, _answer(
        skill_name="long-answers",
        trial={"derivation": "it follows", "literal_ref": {"entry_id": literal}},
    ))
    entry = _entry(env, SAID, "always search the history first")

    assert HELD in _place(env, entry, _derived(entry_id=captured))
    assert HELD in _place(env, entry, _derived(entry_id=queued))


async def test_a_file_keeps_the_words_only_where_an_entry_citing_them_was_placed(
    env: Env,
) -> None:
    notes = env.tray / "context" / "RESPONSIBILITY.md"
    notes.parent.mkdir(parents=True, exist_ok=True)
    notes.write_text("# Lane\n\nThe user said: that answer was wrong.\n\nAn unrelated line.\n")
    destination = ".threads/owner/context/RESPONSIBILITY.md"
    entry = _entry(env, SAID, "always search the history first")
    kept = {"destination": destination, "quote": "The user said: that answer was wrong."}

    assert HELD in _place(env, entry, _derived(**kept))

    unrelated = _entry(env, TURN, "an unrelated note")
    placed = env.continuity._run(THREAD, "place", {
        "entry_id": unrelated, "verdict": "written", **GATE_FIELDS, "destination": destination,
        "quote": "An unrelated line.",
    })
    assert not placed.startswith("Error"), placed
    assert HELD in _place(env, entry, _derived(**kept))

    literal = _entry(env, SAID, "the user said: that answer was wrong")
    placed = env.continuity._run(THREAD, "place", {"entry_id": literal, "verdict": "written", **GATE_FIELDS, **kept})
    assert not placed.startswith("Error"), placed

    assert QUEUED in _place(env, entry, _derived(**kept))
    assert HELD in _place(env, entry, _derived(destination=destination, quote="no such line here"))


async def test_a_trial_file_is_not_where_a_persons_words_are_kept(env: Env) -> None:
    first = _entry(env)
    _place(env, first)
    await _drain(env.worker)
    said = _entry(env, SAID, "citing the head avoids rework")
    _place(env, said, _answer(skill_name="other-trial"))
    entry = _entry(env, SAID, "always search the history first")

    reply = _place(env, entry, _derived(
        destination=".threads/owner/skills/cite-the-head/SKILL.md",
        quote="Name the exact commit a check ran on.",
    ))

    assert HELD in reply


async def test_a_written_trial_is_not_where_a_persons_words_are_kept(env: Env) -> None:
    """A placed trial's entry is retired, so neither it nor its file can be the record."""
    literal = _entry(env, SAID, "the user said: that answer was wrong")
    written = _entry(env, SAID, "prefer the short answer")
    assert QUEUED in _place(env, written, _answer(
        skill_name="short-answers", text="Prefer the short answer.",
        trial={"derivation": "it follows", "literal_ref": {"entry_id": literal}},
    ))
    await _drain(env.worker)
    assert _state(env, written) == ("retired", "written")
    assert len(_offered(env)) == 1
    entry = _entry(env, SAID, "always search the history first")
    its_file = {
        "destination": ".threads/owner/skills/short-answers/SKILL.md",
        "quote": "Prefer the short answer.",
    }

    assert HELD in _place(env, entry, _derived(entry_id=written))
    assert HELD in _place(env, entry, _derived(**its_file))

    trial = _skill(env, "short-answers")
    trial.write_text(trial.read_text().replace("state: trial", "state: retired"))

    assert HELD in _place(env, entry, _derived(entry_id=written))
    assert HELD in _place(env, entry, _derived(**its_file))
    assert QUEUED in _place(env, entry, _derived(entry_id=literal))


# -- a review that was settled, or a version that came back ------------------------


def _trial(env: Env):
    return lesson_files.read_trial(_skill(env).read_text())


def _one_open_review_offers(env: Env) -> None:
    """The file names the lesson's one open review, and the next install offers it."""
    trial = _trial(env)
    [review] = _open(_reviews(env, f"lesson:{trial.lesson.id}:"))
    assert review["key"] == f"lesson:{trial.lesson.id}:{trial.behavioral_sha256}"
    assert trial.lesson.review_ref == f"plan-node:{review['id']}"
    assert [(lesson["id"], lesson["behavioral_sha256"]) for lesson in _offered(env)] == [
        (trial.lesson.id, trial.behavioral_sha256),
    ]


async def test_a_version_that_comes_back_is_offered_under_a_new_review(env: Env) -> None:
    revised = _answer(text="Name the exact commit and branch a check ran on.")
    _place(env, _entry(env))
    await _drain(env.worker)
    first = _trial(env)
    _place(env, _entry(env, [{"kind": "turn", "reference": "turn-19"}], "also the branch"), revised)
    await _drain(env.worker)
    assert _trial(env).behavioral_sha256 != first.behavioral_sha256
    back = _entry(env, [{"kind": "turn", "reference": "turn-30"}], "the branch added nothing")

    _place(env, back)
    await _drain(env.worker)

    assert _trial(env).behavioral_sha256 == first.behavioral_sha256
    assert [row["state"] for row in _reviews(env)] == ["closed", "closed", "pending"]
    _one_open_review_offers(env)
    assert _state(env, back) == ("retired", "written")


async def test_a_trial_placed_again_after_its_review_was_settled_is_offered_again(
    env: Env,
) -> None:
    from plans import closure

    _place(env, _entry(env))
    await _drain(env.worker)
    [review] = _reviews(env)
    closure.close_node(
        review["id"], reason="closed early", actor_id=None, provenance="test", db=env.db,
    )
    env.db.commit()
    assert _offered(env) == []
    again = _entry(env, [{"kind": "turn", "reference": "turn-30"}], "it did help after all")

    _place(env, again)
    await _drain(env.worker)

    assert [row["state"] for row in _reviews(env)] == ["closed", "pending"]
    _one_open_review_offers(env)
    assert _state(env, again) == ("retired", "written")


async def test_the_same_trial_placed_with_another_date_moves_its_reviews_wake(env: Env) -> None:
    _place(env, _entry(env))
    await _drain(env.worker)
    later = _entry(env, [{"kind": "turn", "reference": "turn-30"}], "needs longer")
    due = _due(days=40)

    _place(env, later, _answer(trial={"review_after": due}))
    await _drain(env.worker)

    _one_open_review_offers(env)
    [review] = _reviews(env)
    [watch] = env.db.execute(
        "SELECT condition_value FROM expected_event_watches WHERE plan_node_id=? AND status='armed'",
        (review["id"],),
    ).fetchall()
    assert watch["condition_value"][:10] == due == _trial(env).lesson.review_after[:10]


async def test_a_hypothesis_with_no_boundary_is_captured(env: Env) -> None:
    entry = _entry(env)

    reply = _place(env, entry, _answer(trial={"boundary": None}))
    await _drain(env.worker)

    assert "it has no trigger and boundary" in reply
    assert _offered(env) == [] and len(_open(_reviews(env, "capture:"))) == 1


async def test_appended_evidence_finds_the_same_captured_review(env: Env) -> None:
    entry = _entry(env, SAID)
    _place(env, entry)
    await _drain(env.worker)
    env.continuity._run(THREAD, "replace", {"entry_id": entry, "body": {
        "statement": "citing the head avoids rework", "rationale": "r",
        "source_refs": [*SAID, {"kind": "turn", "reference": "turn-30"}],
    }})

    _place(env, entry)
    await _drain(env.worker)

    [review] = _reviews(env)
    assert review["state"] == "pending"
    [capture] = (env.tray / "context" / "captures").glob("*.md")
    assert [ref["reference"] for ref in lesson_files.read_capture(capture.read_text()).source_refs] == [
        "message 41", "turn-30",
    ]
    assert _artifacts(env, capture) == 1


def _activate_cap_trial(env: Env, number: int, section: str | None, *, owner: str = THREAD) -> Path:
    from core.lesson_trials import Trial
    from plans import lesson_reviews

    name = f"existing-{number}"
    path = env.workspace / ".threads" / owner / "skills" / name / "SKILL.md"
    path.parent.mkdir(parents=True, exist_ok=True)
    text = lesson_files.render_trial(
        lesson_id=f"00000000-0000-0000-0000-{number:012d}", owner=owner, name=name,
        description="Use for checks.", text="Check it.",
        trial=Trial(kind="hypothesis", review_after="2030-01-01T00:00:00+00:00"),
        source_refs=TURN, case_metadata={"new_target_path": "context/shared.md", "target_section": section, "cause": "rule_gap"},
    )
    trial = lesson_files.read_trial(text)
    env.db.execute("INSERT OR IGNORE INTO artifact_identities (id,integration,type,external_id,title,state,created_at,updated_at) VALUES ('cap-artifact','workspace','skill','workspace://cap','cap','active','t','t')")
    node, _ = lesson_reviews.ensure_lesson_review(
        env.db, owner, lesson_id=trial.lesson.id, behavioral_sha256=trial.behavioral_sha256,
        artifact_id="cap-artifact", title=f"Review {name}", acceptance="Review the trial.",
        review_after=trial.lesson.review_after,
    )
    env.db.commit()
    path.write_text(lesson_files.with_review_ref(text, node))
    return path


async def test_trials_on_different_sections_all_run(env: Env) -> None:
    for number in (1, 2):
        _activate_cap_trial(env, number, f"Part {number}")
    reply = _place(env, _entry(env), _answer(new_target_path="context/shared.md", target_section="Part 3"))
    await _drain(env.worker)
    assert "queued" in reply
    assert _skill(env).exists()


@pytest.mark.parametrize(("existing_section", "requested_section", "target", "held"), [
    ("Part A", "Part A", "context/shared.md", True),
    ("Usage", "## usage", "context/shared.md", True),
    ("Part A", "Part B", "context/shared.md", False),
    (None, "Part B", "context/shared.md", True),
    ("Part A", None, "context/shared.md", True),
    (None, None, "context/other.md", False),
])
async def test_trial_section_conflicts(env: Env, existing_section, requested_section, target, held) -> None:
    _activate_cap_trial(env, 1, existing_section)
    answer = _answer(new_target_path=target, target_section=requested_section)
    reply = _place(env, _entry(env), answer)
    await _drain(env.worker)
    if held:
        assert "wait for the conflicting trial on the target file to finish and call place again" in reply
        assert "or set target_section to a section no other trial on the file changes" in reply
        assert "A trial with no section changes the whole file and conflicts with every trial on it" in reply
        assert not _skill(env).exists()
        assert len(list((env.tray / "context" / "captures").glob("*.md"))) == 1
    else:
        assert _skill(env).exists()


def test_unrelated_owner_trials_do_not_hold_this_owner(env: Env) -> None:
    _seed_thread(env.db, "other-owner")
    for number in (1, 2):
        _activate_cap_trial(env, number, f"Part {number}", owner="other-owner")
    reply = _place(env, _entry(env), _answer(new_target_path="context/shared.md", target_section="Part 1"))
    assert "queued" in reply


def test_trial_with_stale_review_does_not_count(env: Env) -> None:
    path = _activate_cap_trial(env, 1, "Part A")
    path.write_text(path.read_text().replace("Check it.", "Changed behavior."))
    reply = _place(env, _entry(env), _answer(new_target_path="context/shared.md", target_section="Part A"))
    assert "queued" in reply


def test_inherited_offered_trial_counts(env: Env) -> None:
    _seed_thread(env.db, "ancestor")
    env.db.execute("UPDATE threads SET spawned_by='ancestor' WHERE id=?", (THREAD,))
    env.db.commit()
    _activate_cap_trial(env, 1, "Part A", owner="ancestor")
    reply = _place(env, _entry(env), _answer(new_target_path="context/shared.md", target_section="Part A"))
    assert "captured hypothesis" in reply


async def test_same_case_merge_uses_heading_key(env: Env) -> None:
    existing = _activate_cap_trial(env, 1, "Usage")
    entry = _entry(env)
    reply = _place(env, entry, _answer(
        case=CASE, cause="rule_gap", new_target_path="context/shared.md", target_section="## usage",
    ))
    assert "queued" in reply
    await _drain(env.worker)
    assert existing.exists() and not _skill(env).exists()
    assert _state(env, entry) == ("retired", "written")


async def test_capture_retry_keeps_target_for_same_case_merge(env: Env) -> None:
    existing = _activate_cap_trial(env, 1, "Part A")
    entry = _entry(env)
    first = _answer(trial={"boundary": None}, case=CASE, cause="rule_gap",
                    new_target_path="context/shared.md", target_section="Part A")
    assert "captured hypothesis" in _place(env, entry, first)
    await _drain(env.worker)
    [capture] = (env.tray / "context" / "captures").glob("*.md")
    assert lesson_files.read_capture(capture.read_text()).case_metadata.get("new_target_path") == "context/shared.md"
    reply = _place(env, entry, _answer())
    assert "queued" in reply
    await _drain(env.worker)
    assert existing.exists() and not _skill(env).exists()
    assert _state(env, entry) == ("retired", "written")


async def test_watch_promotion_retained_target_checks_retired_match(env: Env) -> None:
    from core.lesson_trials import Trial

    retired = _activate_cap_trial(env, 1, "Part A")
    retired.write_text(retired.read_text().replace("state: trial", "state: retired"))
    watch = _skill(env)
    watch.parent.mkdir(parents=True)
    watch.write_text(lesson_files.render_trial(
        lesson_id="00000000-0000-0000-0000-000000000003", owner=THREAD, name="cite-the-head",
        description="Use for checks.", text="Check it.",
        trial=Trial(kind="hypothesis", state="watch", review_after="2030-01-01T00:00:00+00:00"),
        source_refs=TURN, case_metadata={"new_target_path": "context/shared.md", "target_section": "Part A", "cause": "rule_gap"},
    ))
    reply = _place(env, _entry(env), _answer())
    assert "retired trial has the same target" in reply


async def test_revising_a_trial_does_not_conflict_with_itself(env: Env) -> None:
    first = _entry(env)
    answer = _answer(new_target_path="context/shared.md", target_section="Part A")
    _place(env, first, answer)
    await _drain(env.worker)
    assert _skill(env).exists()
    reply = _place(env, _entry(env), answer)
    await _drain(env.worker)
    assert "captured hypothesis" not in reply
    assert lesson_files.read_trial(_skill(env).read_text()).lesson.state == "trial"


async def test_watch_promotion_counts_its_retained_target(env: Env) -> None:
    from core.lesson_trials import Trial

    for number in (1, 2):
        _activate_cap_trial(env, number, f"Part {number}")
    path = _skill(env)
    path.parent.mkdir(parents=True)
    path.write_text(lesson_files.render_trial(
        lesson_id="00000000-0000-0000-0000-000000000003", owner=THREAD, name="cite-the-head",
        description="Use for checks.", text="Check it.",
        trial=Trial(kind="hypothesis", state="watch", review_after="2030-01-01T00:00:00+00:00"),
        source_refs=TURN, case_metadata={"new_target_path": "context/shared.md"},
    ))
    reply = _place(env, _entry(env))
    await _drain(env.worker)
    assert "wait for the conflicting trial on the target file to finish" in reply
    assert lesson_files.read_trial(_skill(env).read_text()).lesson.state == "watch"


@pytest.mark.parametrize(("tiers", "refused"), [(('miss',), False), (('miss', 'serious'), True)])
def test_serious_correction_subject_requires_permanent_placement(env: Env, tiers, refused) -> None:
    from core.correction_learning import record_triage
    from infra.outcome_events import record_outcome_event

    env.db.execute(
        """INSERT INTO active_turns(id,thread_id,execution_epoch,model,status,started_at,boot_epoch,usage_receipts_json)
           VALUES ('turn-serious',?,0,'test/model','running','now','boot','{}')""", (THREAD,),
    )
    for number, tier in enumerate(tiers):
        subject = f"message:correction-{number}"
        env.db.execute(
            """INSERT INTO messages(id,thread_id,role,content,created_at,input_order,active_turn_id)
               VALUES (?,?,'user','Please correct this.','now',?,'turn-serious')""",
            (f"correction-{number}", THREAD, number + 1),
        )
        record_outcome_event(
            env.db, dedupe_key=f"correction:{number}", event_name="measurement.correction", source="agent",
            provenance="agent_recorded", ts="2026-10-20T00:00:00+00:00", subject_key=subject,
            agent_thread_id=THREAD, attributes={"label": "correction"},
        )
        record_triage(env.db, thread_id=THREAD, subject_key=subject, tier=tier, cause="process_gap", new_work="no")
    env.db.commit()
    reply = _place(env, _entry(env))
    if refused:
        assert "serious" in reply and "permanent" in reply
        assert not _skill(env).exists()
    else:
        assert reply.startswith("Entry ") and "queued" in reply


async def test_serious_triage_added_after_capture_is_not_lost_on_retry(env: Env) -> None:
    from core.correction_learning import record_triage
    from infra.outcome_events import record_outcome_event

    env.db.execute(
        """INSERT INTO active_turns(id,thread_id,execution_epoch,model,status,started_at,boot_epoch,usage_receipts_json)
           VALUES ('turn-capture',?,0,'test/model','running','now','boot','{}')""", (THREAD,),
    )
    env.db.execute(
        """INSERT INTO messages(id,thread_id,role,content,created_at,input_order,active_turn_id)
           VALUES ('captured-correction',?,'user','Please correct this.','now',1,'turn-capture')""", (THREAD,),
    )
    env.db.commit()
    entry = _entry(env)
    assert "captured hypothesis" in _place(env, entry, _answer(trial={"boundary": None}))
    await _drain(env.worker)
    subject = "message:captured-correction"
    record_outcome_event(
        env.db, dedupe_key="captured-correction", event_name="measurement.correction", source="agent",
        provenance="agent_recorded", ts="2026-10-20T00:00:00+00:00", subject_key=subject,
        agent_thread_id=THREAD, attributes={"label": "correction"},
    )
    record_triage(env.db, thread_id=THREAD, subject_key=subject, tier="serious", cause="process_gap", new_work="no")
    env.db.execute("UPDATE active_turns SET status='completed' WHERE id='turn-capture'")
    env.db.commit()
    reply = _place(env, entry)
    assert "serious" in reply and "permanent" in reply
    assert not _skill(env).exists()


def test_revision_keeps_the_original_serious_correction(env: Env) -> None:
    from core.correction_learning import record_triage
    from core.lesson_trials import Trial
    from infra.outcome_events import record_outcome_event

    path = _skill(env)
    path.parent.mkdir(parents=True)
    subject = "message:original-correction"
    path.write_text(lesson_files.render_trial(
        lesson_id="00000000-0000-0000-0000-000000000001", owner=THREAD, name="cite-the-head",
        description="Use when reporting a check result on a pull request.", text="Name the commit.",
        trial=Trial(kind="hypothesis", review_after="2030-01-01T00:00:00+00:00"),
        source_refs=TURN, originating_subjects=(subject,),
    ))
    record_outcome_event(
        env.db, dedupe_key="original-correction", event_name="measurement.correction", source="agent",
        provenance="agent_recorded", ts="2026-10-20T00:00:00+00:00", subject_key=subject,
        agent_thread_id=THREAD, attributes={"label": "correction"},
    )
    record_triage(env.db, thread_id=THREAD, subject_key=subject, tier="serious", cause="process_gap", new_work="no")
    env.db.commit()
    reply = _place(env, _entry(env))
    assert "serious" in reply and "permanent" in reply


async def test_a_full_lesson_budget_captures_the_next_trial(env: Env) -> None:
    from plans import public as plans

    env.monkeypatch.setattr("core.lesson_delivery.START_TOKEN_CAP", 1)
    entry = _entry(env)

    reply = _place(env, entry)
    await _drain(env.worker)

    assert "the trial exceeds the task-start guidance budget" in reply
    assert "shorten the trial guidance so it fits at task start" in reply
    assert not _skill(env).exists() and _offered(env) == []
    assert plans.open_capture_review_counts(env.db) == {THREAD: 1}


async def test_duplicate_watch_placement_merges_each_turns_subjects_on_one_record(env: Env) -> None:
    def start(turn_id: str, *message_ids: str) -> None:
        env.db.execute(
            """INSERT INTO active_turns(
                id,thread_id,execution_epoch,model,status,started_at,boot_epoch,usage_receipts_json
            ) VALUES (?,? ,0,'test/model','running','now','boot','{}')""", (turn_id, THREAD),
        )
        first_order = int(env.db.execute(
            "SELECT COALESCE(max(input_order), 0) + 1 FROM messages WHERE thread_id=?", (THREAD,),
        ).fetchone()[0])
        for order, message_id in enumerate(message_ids, first_order):
            env.db.execute(
                """INSERT INTO messages(
                    id,thread_id,role,content,created_at,input_order,active_turn_id
                ) VALUES (?,?,'user','Please correct this.','now',?,?)""",
                (message_id, THREAD, order, turn_id),
            )
        env.db.commit()

    start("turn-one", "first", "second")
    first = _entry(env)
    _place(env, first, _answer(trial={"state": "watch"}))
    await _drain(env.worker)
    watch = lesson_files.read_trial(_skill(env).read_text())
    assert watch.lesson.state == "watch"

    env.db.execute("UPDATE active_turns SET status='completed' WHERE id='turn-one'")
    start("turn-two", "third")
    second = _entry(env, text="the same correction recurred")
    _place(env, second, _answer(duplicate_of="- same lesson"))
    await _drain(env.worker)

    promoted = lesson_files.read_trial(_skill(env).read_text())
    assert promoted.lesson.id == watch.lesson.id
    assert promoted.lesson.state == "trial"
    assert promoted.lesson.review_node_id != watch.lesson.review_node_id
    assert promoted.lesson.originating_subjects == ("message:first", "message:second", "message:third")
    reviews = {review["id"]: review for review in _reviews(env)}
    assert reviews[watch.lesson.review_node_id]["state"] == "closed"
    assert reviews[watch.lesson.review_node_id]["key"] == f"lesson:{watch.lesson.id}:watch:{watch.behavioral_sha256}"
    assert reviews[promoted.lesson.review_node_id]["title"] == "Review trial lesson: cite-the-head"
