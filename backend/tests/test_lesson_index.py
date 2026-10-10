"""A trial lesson is offered only by a newly installed skills index, and only while reviewed.

- A trial enters the index when its owner holds an open review of that exact
  version and its date is ahead. A written file with no review, a fabricated
  or stale review reference, and a passed date each keep it out, with a reason
  on the skills surface and the file untouched.
- Holding a trial never hides the skill it had overridden.
- The installed Skills row lists the offered lessons; the per-turn list keeps
  only each id and behavioral version. A row with no list predates lessons;
  an empty list means none are offered.
- The list is frozen with the install: a restart, a restore and another model
  read the same one, and it never changes the delivered bytes' hash.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
import sqlite3

import pytest

from core import lesson_files, lesson_index
from core.effective_context import ContextSourceEntry, build_effective_context_snapshot
from core.guidance_manifest import ManifestHeader, build_guidance_manifest
from core.installed_context import restore_sources, serialize_sources
from core.lesson_trials import Trial
from core.native_system_context import _proof_only
from core.skills_registry import skill_context_sources
from infra.schema import SCHEMA_SQL
from plans import closure, lesson_reviews
from tests.project_ref_helpers import select_workspace

LESSON = "0f3a6f0e-7f0e-5a55-9d40-6f1f0e6f7a11"
OTHER = "1f3a6f0e-7f0e-5a55-9d40-6f1f0e6f7a22"
HEADER = ManifestHeader("claude-code", "claude-code/sonnet", "claude-sonnet-5", "catalog")
ACCEPTANCE = "The trial is retired, promoted or extended, with the evidence."


def _due(**delta) -> str:
    return (datetime.now(timezone.utc) + timedelta(**delta)).replace(microsecond=0).isoformat()


@pytest.fixture
def db():
    connection = sqlite3.connect(":memory:")
    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA foreign_keys=ON")
    connection.executescript(SCHEMA_SQL)
    connection.execute(
        "INSERT INTO projects (id,name,created_at,updated_at) VALUES ('project','Project','t','t')"
    )
    for thread, parent in (("owner", None), ("child", "owner"), ("other", None)):
        connection.execute(
            """INSERT INTO threads (id,user_id,project_id,model,spawned_by,created_at,updated_at)
               VALUES (?,'user','project','test/model',?,'t','t')""",
            (thread, parent),
        )
    connection.execute(
        """INSERT INTO artifact_identities
           (id,integration,type,external_id,title,state,created_at,updated_at)
           VALUES ('artifact','workspace','skill','workspace://artifact','artifact','active','t','t')"""
    )
    connection.commit()
    yield connection
    connection.close()


@pytest.fixture
def workspace(tmp_path, monkeypatch):
    root = tmp_path / "workspace"
    root.mkdir()
    select_workspace(monkeypatch, root)
    return root


def _path(workspace, thread: str, name: str):
    return workspace / ".threads" / thread / "skills" / name / "SKILL.md"


def _write(workspace, thread: str, name: str, text: str):
    path = _path(workspace, thread, name)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")
    return path


def _trial_text(name: str = "cite-the-head", *, lesson_id: str = LESSON, owner: str = "owner",
                due: str | None = None, text: str = "Name the exact commit the check ran on."):
    return lesson_files.render_trial(
        lesson_id=lesson_id, owner=owner, name=name,
        description="Use when reporting a check result on a pull request.", text=text,
        trial=Trial(kind="hypothesis", review_after=due or _due(days=14),
                    boundary="A one-line typo fix."),
        source_refs=[{"kind": "turn", "reference": "turn-1"}],
    )


def _activate(db, workspace, name: str = "cite-the-head", *, thread: str = "owner", **fields):
    """Write a trial and its review the way activation does; return its path."""
    text = _trial_text(name, owner=thread, **fields)
    trial = lesson_files.read_trial(text)
    node_id, _created = lesson_reviews.ensure_lesson_review(
        db, thread, lesson_id=trial.lesson.id, behavioral_sha256=trial.behavioral_sha256,
        artifact_id="artifact", title=f"Review trial lesson: {name}", acceptance=ACCEPTANCE,
        review_after=trial.lesson.review_after,
    )
    db.commit()
    return _write(workspace, thread, name, lesson_files.with_review_ref(text, node_id)), node_id


def _sources(db, workspace, thread: str = "owner"):
    return skill_context_sources(thread_id=thread, project_id=None, workspace=workspace, db=db)


def _index(sources) -> ContextSourceEntry:
    return next(source for source in sources if source.source_id == "dynamic:skills-index")


def _held(sources, name: str) -> ContextSourceEntry:
    return next(
        source for source in sources
        if source.source_id.startswith("skill-omitted:") and source.metadata["name"] == name
        and source.reason.startswith("trial lesson not offered")
    )


def _plain(name: str, description: str = "Use when this behavior applies.") -> str:
    return f"---\nname: {name}\ndescription: {description}\n---\n# {name}\n\nDo the work.\n"


def test_an_index_with_no_trials_lists_none(db, workspace):
    _write(workspace, "owner", "deploy", _plain("deploy"))

    index = _index(_sources(db, workspace))

    assert "- deploy: " in index.prompt_content
    assert index.metadata["lessons"] == []


def test_an_activated_trial_is_offered_with_its_installed_record(db, workspace):
    path, _node = _activate(db, workspace)
    trial = lesson_files.read_trial(path.read_text(encoding="utf-8"))

    index = _index(_sources(db, workspace))

    line = (
        "- cite-the-head (trial, unproven): Use when reporting a check result on a pull "
        f"request. ({path.resolve()})"
    )
    assert line in index.prompt_content.splitlines()
    assert index.metadata["lessons"] == [{
        "id": LESSON, "behavioral_sha256": trial.behavioral_sha256, "name": "cite-the-head",
        "scope": "thread", "scope_id": "owner", "inherited": False, "path": str(path.resolve()),
        "raw_sha256": lesson_files.raw_sha256(path.read_bytes()),
        "review_after": trial.lesson.review_after, "index_line": line,
    }]


def test_a_written_trial_with_no_review_is_held_with_a_reason(db, workspace):
    path = _write(workspace, "owner", "cite-the-head", _trial_text())
    before = path.read_bytes()

    sources = _sources(db, workspace)

    assert "cite-the-head" not in (_index(sources).prompt_content or "")
    assert _index(sources).metadata["lessons"] == []
    assert _held(sources, "cite-the-head").reason == \
        "trial lesson not offered: trial has no scheduled review"
    assert path.read_bytes() == before


def test_a_fabricated_review_reference_does_not_offer_a_trial(db, workspace):
    _write(workspace, "owner", "cite-the-head",
           lesson_files.with_review_ref(_trial_text(), "no-such-node"))

    sources = _sources(db, workspace)

    assert _index(sources).metadata["lessons"] == []
    assert _held(sources, "cite-the-head").reason == \
        "trial lesson not offered: no review covers this version of the trial"


def test_a_behavioral_edit_holds_the_trial_until_its_new_version_is_reviewed(db, workspace):
    path, node_id = _activate(db, workspace)
    edited = lesson_files.with_review_ref(
        _trial_text(text="Name the branch the check ran on."), node_id,
    )
    path.write_text(edited, encoding="utf-8")

    sources = _sources(db, workspace)

    assert _index(sources).metadata["lessons"] == []
    assert _held(sources, "cite-the-head").reason == \
        "trial lesson not offered: no review covers this version of the trial"


def test_a_settled_or_foreign_review_does_not_offer_a_trial(db, workspace):
    _path_, node_id = _activate(db, workspace)
    closure.close_node(node_id, reason="Retired: no benefit seen", actor_id=None,
                       provenance="test.lesson_index", db=db)
    db.commit()
    foreign, _node = _activate(db, workspace, "other-trial", thread="other", lesson_id=OTHER)
    _write(workspace, "owner", "other-trial",
           foreign.read_text(encoding="utf-8").replace("owner: other", "owner: owner"))

    sources = _sources(db, workspace)

    assert _index(sources).metadata["lessons"] == []
    assert _held(sources, "cite-the-head").reason == \
        "trial lesson not offered: its review is settled"
    assert _held(sources, "other-trial").reason == \
        "trial lesson not offered: its review belongs to another owner"


def test_a_trial_past_its_date_leaves_the_next_index_and_keeps_its_bytes(db, workspace):
    path, _node = _activate(db, workspace, due=_due(seconds=2))
    before = path.read_bytes()
    assert len(_index(_sources(db, workspace)).metadata["lessons"]) == 1

    split_sources = _sources_at(db, workspace, datetime.now(timezone.utc) + timedelta(days=1))

    assert split_sources.lessons == []
    assert [row.errors for row in split_sources.omitted if row.name == "cite-the-head"] == [
        ["trial lesson not offered: trial review date has passed"]
    ]
    assert path.read_bytes() == before


def _sources_at(db, workspace, now, thread: str = "owner"):
    from core.skill_resolution import resolve_effective_skills

    resolved = resolve_effective_skills(
        thread_id=thread, project_id=None, workspace=workspace, db=db,
    )
    return lesson_index.partition(
        resolved.included, resolved.omitted, thread_id=thread, db=db, now=now,
    )


def test_a_parents_trial_is_offered_to_its_child_as_inherited(db, workspace):
    _activate(db, workspace)

    [lesson] = _index(_sources(db, workspace, "child")).metadata["lessons"]

    assert lesson["inherited"] is True
    assert (lesson["scope"], lesson["scope_id"]) == ("thread", "owner")


def test_a_held_trial_does_not_hide_the_skill_it_overrode(db, workspace):
    shared = workspace / "skills" / "cite-the-head" / "SKILL.md"
    shared.parent.mkdir(parents=True)
    shared.write_text(_plain("cite-the-head", "Use the workspace procedure."), encoding="utf-8")
    _write(workspace, "owner", "cite-the-head", _trial_text())

    sources = _sources(db, workspace)

    assert f"- cite-the-head: Use the workspace procedure. ({shared.resolve()})" in \
        _index(sources).prompt_content
    assert _held(sources, "cite-the-head").stable_reference == \
        str(_path(workspace, "owner", "cite-the-head").resolve())
    assert not [
        source for source in sources
        if source.reason == "overridden by a more specific skill"
    ]


def test_only_a_trial_is_marked_on_the_index_and_the_marked_line_is_the_one_counted(
    db, workspace,
):
    _write(workspace, "owner", "deploy", _plain("deploy"))
    _activate(db, workspace)

    index = _index(_sources(db, workspace))

    lines = index.prompt_content.splitlines()
    [lesson] = index.metadata["lessons"]
    assert [line for line in lines if lesson_index.TRIAL_MARK in line] == [lesson["index_line"]]
    assert lesson["index_line"].startswith("- cite-the-head (trial, unproven): ")
    assert [line for line in lines if line.startswith("- deploy")][0].startswith("- deploy: ")
    skills_tokens = index.to_inventory_dict()["prompt_tokens"]
    assert lesson_index.descriptor_estimates([lesson], skills_tokens=skills_tokens) == {
        LESSON: lesson_index.line_tokens(lesson["index_line"]),
    }
    assert lesson_index.line_tokens(lesson["index_line"]) <= skills_tokens


def _shared(workspace, name: str = "cite-the-head"):
    shared = workspace / "skills" / name / "SKILL.md"
    shared.parent.mkdir(parents=True)
    shared.write_text(_plain(name, "Use the workspace procedure."), encoding="utf-8")
    return shared


def test_an_expired_trial_does_not_displace_an_ordinary_skill_of_its_name(db, workspace):
    _activate(db, workspace, due=_due(seconds=2))
    shared = _shared(workspace)

    split = _sources_at(db, workspace, datetime.now(timezone.utc) + timedelta(days=1))

    assert [(row.name, row.path) for row in split.offered if row.name == "cite-the-head"] == [
        ("cite-the-head", shared.resolve()),
    ]
    assert split.lessons == []


def test_a_retired_trial_does_not_displace_an_ordinary_skill_of_its_name(db, workspace):
    path, _node = _activate(db, workspace)
    path.write_text(path.read_text().replace("state: trial", "state: retired"))
    shared = _shared(workspace)

    sources = _sources(db, workspace)

    assert f"- cite-the-head: Use the workspace procedure. ({shared.resolve()})" in \
        _index(sources).prompt_content.splitlines()
    assert _index(sources).metadata["lessons"] == []
    assert _held(sources, "cite-the-head").reason == "trial lesson not offered: lesson is retired"


SAME_NAME = "trial lesson not offered: an ordinary skill has its name"


def test_a_workspace_skill_that_takes_an_offered_trials_name_stops_the_trial(db, workspace):
    path, node_id = _activate(db, workspace)
    assert [lesson["name"] for lesson in _index(_sources(db, workspace)).metadata["lessons"]] == [
        "cite-the-head",
    ]
    trial_bytes = path.read_bytes()
    shared = _shared(workspace)
    shared_bytes = shared.read_bytes()

    sources = _sources(db, workspace)

    index = _index(sources)
    assert [line for line in index.prompt_content.splitlines() if "cite-the-head" in line] == [
        f"- cite-the-head: Use the workspace procedure. ({shared.resolve()})",
    ]
    assert index.metadata["lessons"] == []
    held = _held(sources, "cite-the-head")
    assert (held.reason, held.stable_reference) == (SAME_NAME, str(path.resolve()))
    assert not [
        source for source in sources if source.reason == "overridden by a more specific skill"
    ]
    assert (path.read_bytes(), shared.read_bytes()) == (trial_bytes, shared_bytes)
    assert lesson_reviews.lesson_review_refusal(
        db, node_id, owner_thread_id="owner", lesson_id=LESSON,
        behavioral_sha256=lesson_files.read_trial(trial_bytes.decode()).behavioral_sha256,
    ) is None

    shared.unlink()

    assert [lesson["name"] for lesson in _index(_sources(db, workspace)).metadata["lessons"]] == [
        "cite-the-head",
    ]


def test_a_project_skill_that_takes_an_offered_trials_name_stops_the_trial(db, workspace):
    from core.skill_resolution import thread_dir
    from tests.project_ref_helpers import project_ref, write_project_context

    project = write_project_context(workspace, "p")
    db.execute(
        "INSERT INTO projects (id,name,path,created_at,updated_at) VALUES ('p','P',?,'t','t')",
        (project_ref("p"),),
    )
    db.execute("UPDATE threads SET project_id='p' WHERE id='owner'")
    text = _trial_text()
    trial = lesson_files.read_trial(text)
    node_id, _created = lesson_reviews.ensure_lesson_review(
        db, "owner", lesson_id=LESSON, behavioral_sha256=trial.behavioral_sha256,
        artifact_id="artifact", title="Review trial lesson: cite-the-head",
        acceptance=ACCEPTANCE, review_after=trial.lesson.review_after,
    )
    db.commit()
    path = thread_dir("owner", project_dir=project, workspace=workspace) / \
        "skills" / "cite-the-head" / "SKILL.md"
    path.parent.mkdir(parents=True)
    path.write_text(lesson_files.with_review_ref(text, node_id), encoding="utf-8")

    def sources():
        return skill_context_sources(
            thread_id="owner", project_id="p", workspace=workspace, db=db,
        )

    assert [lesson["name"] for lesson in _index(sources()).metadata["lessons"]] == [
        "cite-the-head",
    ]
    taken = project / "skills" / "cite-the-head" / "SKILL.md"
    taken.parent.mkdir(parents=True)
    taken.write_text(_plain("cite-the-head", "Use the project procedure."), encoding="utf-8")

    after = sources()

    assert [line for line in _index(after).prompt_content.splitlines()
            if "cite-the-head" in line] == [
        f"- cite-the-head: Use the project procedure. ({taken.resolve()})",
    ]
    assert _index(after).metadata["lessons"] == []
    assert _held(after, "cite-the-head").reason == SAME_NAME
    assert path.exists() and taken.exists()


def test_a_system_skill_that_takes_an_offered_trials_name_stops_the_trial(db, workspace):
    system = next(
        row for row in _sources_at(db, workspace, datetime.now(timezone.utc)).offered
        if row.scope == "system"
    )
    path, _node = _activate(db, workspace, system.name)

    sources = _sources(db, workspace)

    index = _index(sources)
    assert [line for line in index.prompt_content.splitlines()
            if line.startswith(f"- {system.name}")] == [lesson_index.index_line(system)]
    assert index.metadata["lessons"] == []
    assert [
        source.reason for source in sources
        if source.source_id.startswith("skill-omitted:")
        and source.stable_reference == str(path.resolve())
    ] == ["shadowed by System"]
    assert path.exists() and system.path.exists()


def test_a_child_does_not_inherit_a_trial_an_ordinary_skill_took_the_name_of(db, workspace):
    _activate(db, workspace)
    shared = _shared(workspace)

    index = _index(_sources(db, workspace, "child"))

    assert f"- cite-the-head: Use the workspace procedure. ({shared.resolve()})" in \
        index.prompt_content.splitlines()
    assert index.metadata["lessons"] == []


def test_a_trial_over_another_trial_of_its_name_is_not_held_for_the_name(db, workspace):
    _activate(db, workspace)
    _activate(db, workspace, thread="child", lesson_id=OTHER)

    lessons = _index(_sources(db, workspace, "child")).metadata["lessons"]

    assert [(lesson["id"], lesson["scope_id"]) for lesson in lessons] == [(OTHER, "child")]


def _named(index: ContextSourceEntry, name: str = "cite-the-head") -> list[str]:
    return [line for line in index.prompt_content.splitlines() if line.startswith(f"- {name}")]


def _held_reasons(sources, name: str = "cite-the-head") -> dict[str, str]:
    """Why each trial of this name is held, by the thread whose tray holds it."""
    return {
        source.stable_reference.split("/.threads/")[1].split("/")[0]: source.reason
        for source in sources
        if source.source_id.startswith("skill-omitted:") and source.metadata["name"] == name
        and "/.threads/" in source.stable_reference
    }


@pytest.mark.parametrize("ancestor", ["active", "expired", "unreviewed"])
def test_an_ordinary_skill_under_two_trials_of_its_name_is_the_one_offered(
    db, workspace, ancestor,
):
    shared = _shared(workspace)
    if ancestor == "unreviewed":
        _write(workspace, "owner", "cite-the-head", _trial_text())
    else:
        _activate(db, workspace, due=_due(days=14 if ancestor == "active" else -1))
    _activate(db, workspace, thread="child", lesson_id=OTHER)

    sources = _sources(db, workspace, "child")

    index = _index(sources)
    assert _named(index) == [f"- cite-the-head: Use the workspace procedure. ({shared.resolve()})"]
    assert index.metadata["lessons"] == []
    assert _held_reasons(sources) == {
        "child": SAME_NAME,
        "owner": {
            "active": SAME_NAME,
            "expired": "trial lesson not offered: trial review date has passed",
            "unreviewed": "trial lesson not offered: trial has no scheduled review",
        }[ancestor],
    }


@pytest.mark.parametrize("ancestor", ["expired", "unreviewed"])
def test_a_held_trial_never_gives_way_to_a_trial_that_would_itself_be_held(
    db, workspace, ancestor,
):
    if ancestor == "unreviewed":
        _write(workspace, "owner", "cite-the-head", _trial_text())
    else:
        _activate(db, workspace, due=_due(days=-1))
    _activate(db, workspace, thread="child", lesson_id=OTHER, due=_due(days=-1))

    sources = _sources(db, workspace, "child")

    assert _named(_index(sources)) == []
    assert _index(sources).metadata["lessons"] == []
    assert sorted(_held_reasons(sources)) == ["child", "owner"]
    assert not [
        source for source in sources if source.reason == "overridden by a more specific skill"
    ]


def test_a_held_trial_gives_way_to_an_eligible_trial_with_its_lesson_record(db, workspace):
    path, _node = _activate(db, workspace)
    _activate(db, workspace, thread="child", lesson_id=OTHER, due=_due(days=-1))

    sources = _sources(db, workspace, "child")

    index = _index(sources)
    [lesson] = index.metadata["lessons"]
    assert (lesson["id"], lesson["scope_id"], lesson["inherited"]) == (LESSON, "owner", True)
    assert _named(index) == [lesson["index_line"]]
    assert lesson["path"] == str(path.resolve())
    assert _held_reasons(sources) == {
        "child": "trial lesson not offered: trial review date has passed",
    }


def test_a_trial_under_one_the_budget_holds_is_not_offered_in_its_place(
    db, workspace, monkeypatch,
):
    _activate(db, workspace)
    _activate(db, workspace, thread="child", lesson_id=OTHER)
    monkeypatch.setattr(lesson_index, "INDEX_TOKEN_BUDGET", 1)

    sources = _sources(db, workspace, "child")

    assert _named(_index(sources)) == []
    assert _index(sources).metadata["lessons"] == []
    assert _held_reasons(sources)["child"] == \
        "trial lesson not offered: the lesson index budget is full"


MISPLACED = "trial lesson not offered: its file is not in its owner's skills"


def _copy_reasons(sources, copy) -> list[str]:
    return [
        source.reason for source in sources
        if source.source_id.startswith("skill-omitted:")
        and source.stable_reference == str(copy.resolve())
    ]


def _still_the_owners(db, workspace, path, node_id, before: bytes) -> None:
    """The owner's file and review are as they were, and the owner is still offered it."""
    assert path.read_bytes() == before
    assert db.execute("SELECT state FROM plan_nodes WHERE id=?", (node_id,)).fetchone()[0] == \
        "pending"
    [lesson] = _index(_sources(db, workspace)).metadata["lessons"]
    assert (lesson["path"], lesson["scope_id"], lesson["inherited"]) == (
        str(path.resolve()), "owner", False,
    )


def test_a_copy_of_a_trial_in_another_threads_skills_is_not_offered(db, workspace):
    path, node_id = _activate(db, workspace)
    before = path.read_bytes()
    copy = _write(workspace, "other", "cite-the-head", before.decode())

    sources = _sources(db, workspace, "other")

    assert _index(sources).metadata["lessons"] == []
    assert _named(_index(sources)) == []
    assert _copy_reasons(sources, copy) == [MISPLACED]
    assert copy.read_bytes() == before
    _still_the_owners(db, workspace, path, node_id, before)


def test_a_copy_of_a_trial_in_the_workspace_skills_is_not_offered(db, workspace):
    path, node_id = _activate(db, workspace)
    before = path.read_bytes()
    copy = workspace / "skills" / "cite-the-head" / "SKILL.md"
    copy.parent.mkdir(parents=True)
    copy.write_bytes(before)

    sources = _sources(db, workspace, "other")

    assert _index(sources).metadata["lessons"] == []
    assert _named(_index(sources)) == []
    assert _copy_reasons(sources, copy) == [MISPLACED]
    _still_the_owners(db, workspace, path, node_id, before)
    [lesson] = _index(_sources(db, workspace, "child")).metadata["lessons"]
    assert (lesson["path"], lesson["inherited"]) == (str(path.resolve()), True)


def test_a_copy_of_a_trial_in_the_project_skills_is_not_offered(db, workspace):
    from core.skill_resolution import thread_dir
    from tests.project_ref_helpers import project_ref, write_project_context

    project = write_project_context(workspace, "p")
    db.execute(
        "INSERT INTO projects (id,name,path,created_at,updated_at) VALUES ('p','P',?,'t','t')",
        (project_ref("p"),),
    )
    db.execute("UPDATE threads SET project_id='p' WHERE id IN ('owner','other')")
    text = _trial_text()
    trial = lesson_files.read_trial(text)
    node_id, _created = lesson_reviews.ensure_lesson_review(
        db, "owner", lesson_id=LESSON, behavioral_sha256=trial.behavioral_sha256,
        artifact_id="artifact", title="Review trial lesson: cite-the-head",
        acceptance=ACCEPTANCE, review_after=trial.lesson.review_after,
    )
    db.commit()
    path = thread_dir("owner", project_dir=project, workspace=workspace) / \
        "skills" / "cite-the-head" / "SKILL.md"
    path.parent.mkdir(parents=True)
    path.write_text(lesson_files.with_review_ref(text, node_id), encoding="utf-8")
    copy = project / "skills" / "cite-the-head" / "SKILL.md"
    copy.parent.mkdir(parents=True)
    copy.write_bytes(path.read_bytes())

    def sources(thread: str):
        return skill_context_sources(thread_id=thread, project_id="p", workspace=workspace, db=db)

    other = sources("other")

    assert _index(other).metadata["lessons"] == []
    assert _named(_index(other)) == []
    assert _copy_reasons(other, copy) == [MISPLACED]
    [lesson] = _index(sources("owner")).metadata["lessons"]
    assert (lesson["path"], lesson["scope_id"]) == (str(path.resolve()), "owner")


def test_a_full_lesson_budget_keeps_the_earlier_trial_and_holds_the_newer(
    db, workspace, monkeypatch,
):
    first, _node = _activate(db, workspace)
    line = lesson_index.index_line(
        next(row for row in _sources_at(db, workspace, datetime.now(timezone.utc)).offered
             if row.name == "cite-the-head")
    )
    monkeypatch.setattr(lesson_index, "INDEX_TOKEN_BUDGET", lesson_index.line_tokens(line))
    db.execute("UPDATE plan_nodes SET created_at='2026-01-01T00:00:00+00:00'")
    _activate(db, workspace, "a-newer-trial", lesson_id=OTHER)

    sources = _sources(db, workspace)

    assert [lesson["name"] for lesson in _index(sources).metadata["lessons"]] == ["cite-the-head"]
    assert _held(sources, "a-newer-trial").reason == \
        "trial lesson not offered: the lesson index budget is full"
    assert first.exists()


def test_a_trial_whose_review_cannot_be_checked_is_held(workspace):
    bare = sqlite3.connect(":memory:")
    bare.row_factory = sqlite3.Row
    bare.execute("CREATE TABLE threads (id TEXT PRIMARY KEY, spawned_by TEXT, parent_thread_id TEXT)")
    _write(workspace, "owner", "cite-the-head",
           lesson_files.with_review_ref(_trial_text(), "node-1"))

    for database in (bare, None):
        sources = _sources(database, workspace)
        assert _index(sources).metadata["lessons"] == []
        assert _held(sources, "cite-the-head").reason == \
            "trial lesson not offered: its review could not be checked"


def _snapshot(sources, model: str = "claude-code/sonnet"):
    return build_effective_context_snapshot(
        thread_id="owner", reason="test", scope_facts={}, agent_facts={},
        harness_model_facts={"harness": "claude-code", "model": model},
        cwd=None, source_root=None, sources=sources, history=None, current_user_content="hi",
    )


def _skills_entry(sources, model: str = "claude-code/sonnet") -> dict:
    manifest = build_guidance_manifest(_snapshot(sources, model), HEADER)
    return next(entry for entry in manifest.entries if entry["source_id"] == "dynamic:skills-index")


def test_the_per_turn_list_keeps_only_each_lessons_id_and_version(db, workspace):
    path, _node = _activate(db, workspace)
    trial = lesson_files.read_trial(path.read_text(encoding="utf-8"))

    entry = _skills_entry(_sources(db, workspace))

    assert entry["lessons"] == [{"id": LESSON, "behavioral_sha256": trial.behavioral_sha256}]


def test_no_list_means_unknown_and_an_empty_list_means_none(db, workspace):
    _write(workspace, "owner", "deploy", _plain("deploy"))
    sources = _sources(db, workspace)
    index = _index(sources)
    older = ContextSourceEntry(**{
        **{name: getattr(index, name) for name in (
            "source_id", "origin_section", "label", "stable_reference", "freshness",
            "inclusion", "reason", "owner_surface", "prompt_content")},
        "metadata": {key: value for key, value in index.metadata.items() if key != "lessons"},
    })

    assert _skills_entry(sources)["lessons"] == []
    assert "lessons" not in _skills_entry([older])
    assert _skills_entry([older])["delivered_sha256"] == _skills_entry(sources)["delivered_sha256"]


def test_only_the_skills_row_projects_lessons():
    other = ContextSourceEntry(
        source_id="dynamic:other", origin_section="dynamic", label="Other",
        stable_reference="other", freshness="fresh", inclusion="included", reason="row",
        owner_surface="x", prompt_content="text",
        metadata={"lessons": [{"id": LESSON, "behavioral_sha256": "a" * 64}]},
    )

    [entry] = build_guidance_manifest(_snapshot([other]), HEADER).entries

    assert "lessons" not in entry


def test_the_installed_list_survives_restore_restart_and_another_model(db, workspace):
    path, _node = _activate(db, workspace)
    installed = _sources(db, workspace)
    first = _skills_entry(installed)

    restored = restore_sources(serialize_sources(installed))
    path.write_text(_plain("cite-the-head"), encoding="utf-8")
    _activate(db, workspace, "a-newer-trial", lesson_id=OTHER)

    assert _index(restored).metadata["lessons"] == _index(installed).metadata["lessons"]
    assert _skills_entry(restored) == first
    assert _skills_entry(restored, "codex/gpt-5.6-terra")["lessons"] == first["lessons"]
    assert _skills_entry(restored, "codex/gpt-5.6-terra")["delivered_sha256"] == \
        first["delivered_sha256"]
    assert len(_index(_sources(db, workspace)).metadata["lessons"]) == 1
    assert _index(_sources(db, workspace)).metadata["lessons"][0]["id"] == OTHER


def test_a_native_proof_row_still_projects_its_lessons(db, workspace):
    _activate(db, workspace)
    proof = [_proof_only(source, reason="component proof") for source in _sources(db, workspace)]

    entry = _skills_entry(proof)

    assert entry["inclusion"] == "proof"
    assert [lesson["id"] for lesson in entry["lessons"]] == [LESSON]


def test_per_lesson_estimates_never_exceed_the_skills_total(db, workspace):
    _activate(db, workspace)
    _activate(db, workspace, "a-newer-trial", lesson_id=OTHER)
    index = _index(_sources(db, workspace))
    lessons = index.metadata["lessons"]
    skills_tokens = index.to_inventory_dict()["prompt_tokens"]

    estimates = lesson_index.descriptor_estimates(lessons, skills_tokens=skills_tokens)

    assert set(estimates) == {LESSON, OTHER}
    assert 0 < sum(estimates.values()) <= skills_tokens
    assert sum(lesson_index.descriptor_estimates(lessons, skills_tokens=10).values()) <= 10
