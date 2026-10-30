"""measure_sync ownership: an agent-level file syncs from the caller's own folder; a workflow file
syncs only for that workflow's owner among the caller's effective workflow skills; a namespace is
held by whoever's live events are in it, and frees when they leave.
"""
from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

from core.skill_scanning import scan_scope
from core.skills_registry import SkillResolution
from infra.measure_registry import REGISTRY, sync
from infra.outcome_events_schema import OUTCOME_EVENTS_SCHEMA_SQL
from tests.skill_sidecar_helpers import POINTER
from tests.test_measure_registry import MEASURES, OWNER, deciding, own, small
from tools import workflow_task_fields
from tools.workflow_owners import workflow_owners

OTHER = "agent-evil"
PROJECT = "proj-1"
STEPS = "  steps:\n    - {id: review, title: Review it, required: true, check: reviewed}\n"


def db() -> sqlite3.Connection:
    connection = sqlite3.connect(":memory:")
    connection.executescript(OUTCOME_EVENTS_SCHEMA_SQL)
    connection.execute("CREATE TABLE threads (id TEXT PRIMARY KEY, project_id TEXT, lifecycle_state TEXT, archived_reason TEXT)")
    for thread in (OWNER, OTHER):
        connection.execute("INSERT INTO threads VALUES (?,?, 'active', NULL)", (thread, PROJECT))
    REGISTRY.reset()
    return connection


def definitions(connection: sqlite3.Connection) -> int:
    return connection.execute("SELECT count(*) FROM measure_definitions").fetchone()[0]


def skill(root: Path, name: str, measures: str, *, owner: str | None = None, steps: bool = True) -> Path:
    """A workflow skill under ``root``/skills whose steps live in its corsia.yaml sidecar."""
    folder = root / "skills" / name
    folder.mkdir(parents=True, exist_ok=True)
    (folder / "SKILL.md").write_text(f"---\nname: {name}\ndescription: A workflow.\n{POINTER}---\n# {name}\n")
    sidecar = "workflow:\n" + (f"  owner: {owner}\n" if owner else "") + (STEPS if steps else "  notes: none\n")
    (folder / "corsia.yaml").write_text(sidecar)
    (folder / "measures.yaml").write_text(measures)
    return folder / "measures.yaml"


@pytest.fixture
def effective(monkeypatch: pytest.MonkeyPatch):
    """Set the caller's effective skills to the scanned scopes the test lists."""
    scopes: list = []
    monkeypatch.setattr(workflow_task_fields, "resolve_effective_skills", lambda **_: SkillResolution([row for scope in scopes for row in scope], [], []))
    return scopes


def sync_as(connection: sqlite3.Connection, agent: str, path: Path, text: str) -> dict:
    return sync(connection, owner_thread_id=agent, path=str(path), text=text, workflows=workflow_owners(agent, PROJECT, connection))


TEXT = small(deciding())


def test_a_thread_scope_workflow_syncs_for_the_thread_it_belongs_to(tmp_path: Path, effective: list) -> None:
    root = tmp_path / ".threads" / OWNER
    path = skill(root, "llm-pr-review", TEXT)
    effective.append(scan_scope(root, "thread", OWNER))
    connection = db()
    assert sync_as(connection, OWNER, path, TEXT)["namespace"] == "qa"


def test_a_sidecar_defined_project_workflow_syncs_for_the_owner_its_corsia_yaml_names(tmp_path: Path, effective: list) -> None:
    root = tmp_path / "project"
    path = skill(root, "llm-pr-review", TEXT, owner=OWNER)
    effective.append(scan_scope(root, "project", PROJECT))
    connection = db()
    assert {item["status"] for item in sync_as(connection, OWNER, path, TEXT)["items"]} == {"added"}


def test_a_project_workflow_with_no_owner_is_refused(tmp_path: Path, effective: list) -> None:
    root = tmp_path / "project"
    path = skill(root, "llm-pr-review", TEXT)
    effective.append(scan_scope(root, "project", PROJECT))
    connection = db()
    with pytest.raises(ValueError, match=r"^measure_sync: workflow llm-pr-review has no owner\. Add `owner: <your thread id>` under `workflow:` in its corsia\.yaml\."):
        sync_as(connection, OWNER, path, TEXT)
    assert definitions(connection) == 0


def test_a_project_workflow_owned_by_another_agent_is_refused(tmp_path: Path, effective: list) -> None:
    root = tmp_path / "project"
    path = skill(root, "llm-pr-review", TEXT, owner=OWNER)
    effective.append(scan_scope(root, "project", PROJECT))
    connection = db()
    with pytest.raises(ValueError, match=r"workflow llm-pr-review is owned by another agent \(thread agent-qg\); only its owner can use it"):
        sync_as(connection, OTHER, path, TEXT)
    assert definitions(connection) == 0


def test_a_thread_skill_whose_owner_key_names_another_thread_is_refused(tmp_path: Path, effective: list) -> None:
    root = tmp_path / ".threads" / OWNER
    path = skill(root, "llm-pr-review", TEXT, owner=OTHER)
    effective.append(scan_scope(root, "thread", OWNER))
    connection = db()
    for agent in (OWNER, OTHER):
        with pytest.raises(ValueError, match=r"is in thread agent-qg's own skills, but its owner key names thread agent-evil"):
            sync_as(connection, agent, path, TEXT)
    assert definitions(connection) == 0


def test_a_skill_planted_outside_the_callers_effective_skills_is_refused(tmp_path: Path, effective: list) -> None:
    """A SKILL.md the caller wrote somewhere that is not one of its scopes, naming itself as owner."""
    planted = skill(tmp_path / "anywhere", "llm-pr-review", TEXT, owner=OTHER)
    connection = db()
    with pytest.raises(ValueError, match=r"workflow llm-pr-review is not one of your workflow skills \(yours: none\)"):
        sync_as(connection, OTHER, planted, TEXT)
    assert definitions(connection) == 0


def test_a_skill_without_workflow_steps_is_not_a_workflow(tmp_path: Path, effective: list) -> None:
    root = tmp_path / ".threads" / OWNER
    path = skill(root, "llm-pr-review", TEXT, steps=False)
    effective.append(scan_scope(root, "thread", OWNER))
    with pytest.raises(ValueError, match="is not one of your workflow skills"):
        sync_as(db(), OWNER, path, TEXT)


def test_a_workflow_file_must_sit_beside_its_skill(tmp_path: Path, effective: list) -> None:
    root = tmp_path / ".threads" / OWNER
    skill(root, "llm-pr-review", TEXT)
    effective.append(scan_scope(root, "thread", OWNER))
    elsewhere = tmp_path / ".threads" / OWNER / "notes" / "measures.yaml"
    with pytest.raises(ValueError, match=r"workflow llm-pr-review's measures\.yaml syncs only from beside its SKILL\.md"):
        sync_as(db(), OWNER, elsewhere, TEXT)


def test_an_agent_level_file_in_another_agents_folder_is_refused_and_claims_nothing() -> None:
    connection = db()
    with pytest.raises(ValueError, match=r"an agent-level measures.yaml syncs only from \.threads/agent-evil/measures\.yaml, not /ws/\.threads/agent-qg/measures\.yaml"):
        sync(connection, owner_thread_id=OTHER, path=own(OWNER), text=MEASURES)
    assert definitions(connection) == 0
    assert sync(connection, owner_thread_id=OWNER, path=own(OWNER), text=MEASURES)["namespace"] == "mail"


def test_an_agent_level_file_outside_any_threads_folder_is_refused() -> None:
    connection = db()
    for path in ("/tmp/measures.yaml", f"/ws/{OWNER}/measures.yaml", f"/ws/.threads/{OWNER}/notes/measures.yaml"):
        with pytest.raises(ValueError, match="an agent-level measures.yaml syncs only from"):
            sync(connection, owner_thread_id=OWNER, path=path, text=MEASURES)
    assert definitions(connection) == 0


def test_a_namespace_frees_when_its_holders_file_stops_declaring_it() -> None:
    connection = db()
    sync(connection, owner_thread_id=OWNER, path=own(OWNER), text=MEASURES)
    with pytest.raises(ValueError, match=r'Namespace "mail" belongs to another agent \(thread agent-qg\)'):
        sync(connection, owner_thread_id=OTHER, path=own(OTHER), text=MEASURES)
    moved = MEASURES.replace("namespace: mail", "namespace: post").replace("mail.triaged", "post.triaged")
    sync(connection, owner_thread_id=OWNER, path=own(OWNER), text=moved)  # mail.triaged is now removed
    result = sync(connection, owner_thread_id=OTHER, path=own(OTHER), text=MEASURES)
    assert result["namespace"] == "mail" and "namespace_takeover" not in result
    assert REGISTRY.events["mail.triaged"].owner == OTHER and REGISTRY.events["post.triaged"].owner == OWNER


@pytest.mark.parametrize("state, reason", [("purged", None), ("archived", "user"), (None, None)])
def test_a_departed_holders_namespace_is_taken_over_and_only_its_events_there_retire(state: str | None, reason: str | None) -> None:
    connection = db()
    both = MEASURES.replace("kpis:", "  mail.sorted:\n    attributes: {message_ref: id}\n    dedupe: [message_ref]\nkpis:")
    sync(connection, owner_thread_id=OWNER, path=own(OWNER), text=both)
    if state is None:
        connection.execute("DELETE FROM threads WHERE id=?", (OWNER,))
    else:
        connection.execute("UPDATE threads SET lifecycle_state=?, archived_reason=? WHERE id=?", (state, reason, OWNER))
    connection.execute(
        "INSERT INTO measure_definitions (owner_thread_id,kind,name,version,spec_json,file_path,file_sha256,synced_at,removed) "
        "VALUES (?, 'event', 'shop.triaged', 1, ?, '/elsewhere', 'sha', 't', 0)",
        (OWNER, '{"attributes":{"message_ref":"id"},"dedupe":["message_ref"]}'),
    )
    result = sync(connection, owner_thread_id=OTHER, path=own(OTHER), text=MEASURES)
    assert result["namespace_takeover"] == [f"took over namespace mail from {OWNER}; 2 events retired"]
    assert REGISTRY.events["mail.triaged"].owner == OTHER and "mail.sorted" not in REGISTRY.events
    assert REGISTRY.events["shop.triaged"].owner == OWNER  # outside the namespace: untouched


def test_a_dormant_or_agent_archived_holder_still_holds() -> None:
    connection = db()
    sync(connection, owner_thread_id=OWNER, path=own(OWNER), text=MEASURES)
    for state, reason in (("dormant", None), ("archived", "dormancy")):
        connection.execute("UPDATE threads SET lifecycle_state=?, archived_reason=? WHERE id=?", (state, reason, OWNER))
        with pytest.raises(ValueError, match="belongs to another agent"):
            sync(connection, owner_thread_id=OTHER, path=own(OTHER), text=MEASURES)
