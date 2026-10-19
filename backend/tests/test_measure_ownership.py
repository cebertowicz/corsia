"""measure_sync binds ownership to the path: whose folder a file is in, and which skill and workflow it names."""
from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

from infra.measure_registry import REGISTRY, sync
from infra.outcome_events_schema import OUTCOME_EVENTS_SCHEMA_SQL
from tests.test_measure_registry import MEASURES, OWNER, deciding, own, small

OTHER = "agent-evil"


def db() -> sqlite3.Connection:
    connection = sqlite3.connect(":memory:")
    connection.executescript(OUTCOME_EVENTS_SCHEMA_SQL)
    REGISTRY.reset()
    return connection


def claims(connection: sqlite3.Connection) -> tuple[list[tuple], list[tuple], int]:
    return (
        connection.execute("SELECT namespace, owner_thread_id FROM measure_namespaces ORDER BY namespace").fetchall(),
        connection.execute("SELECT workflow, owner_thread_id FROM measure_workflows ORDER BY workflow").fetchall(),
        connection.execute("SELECT count(*) FROM measure_definitions").fetchone()[0],
    )


def skill(root: Path, agent: str, name: str, text: str, *, skill_name: str | None = None) -> str:
    """A workflow skill folder inside ``agent``'s own .threads folder whose SKILL.md names ``agent`` as owner."""
    folder = root / ".threads" / agent / "skills" / name
    folder.mkdir(parents=True, exist_ok=True)
    (folder / "SKILL.md").write_text(f"---\nname: {skill_name or name}\ncorsia:\n  workflow:\n    owner: {agent}\n---\n")
    (folder / "measures.yaml").write_text(text)
    return str(folder / "measures.yaml")


def test_an_agent_level_file_in_another_agents_folder_is_refused_and_claims_nothing() -> None:
    connection = db()
    with pytest.raises(ValueError, match=r"an agent-level measures.yaml syncs only from \.threads/agent-evil/measures\.yaml, not /ws/\.threads/agent-qg/measures\.yaml"):
        sync(connection, owner_thread_id=OTHER, path=own(OWNER), text=MEASURES)
    assert claims(connection) == ([], [], 0)
    result = sync(connection, owner_thread_id=OWNER, path=own(OWNER), text=MEASURES)  # the real owner is unaffected
    assert result["namespace"] == "mail" and claims(connection)[0] == [("mail", OWNER)]


def test_an_agent_level_file_outside_any_threads_folder_is_refused() -> None:
    connection = db()
    for path in ("/tmp/measures.yaml", f"/ws/{OWNER}/measures.yaml", f"/ws/.threads/{OWNER}/notes/measures.yaml"):
        with pytest.raises(ValueError, match="an agent-level measures.yaml syncs only from"):
            sync(connection, owner_thread_id=OWNER, path=path, text=MEASURES)
    assert claims(connection) == ([], [], 0)


def test_a_self_written_skill_under_someone_elses_workflow_name_is_refused_and_the_owner_still_syncs(tmp_path: Path) -> None:
    connection = db()
    text = small(deciding())
    mine = skill(tmp_path, OWNER, "llm-pr-review", text)
    sync(connection, owner_thread_id=OWNER, path=mine, text=text)
    assert claims(connection)[1] == [("llm-pr-review", OWNER)]

    forged = text.replace("namespace: qa", "namespace: qz").replace("qa.done", "qz.done")
    theirs = skill(tmp_path, OTHER, "llm-pr-review", forged)  # their own SKILL.md names them as owner
    with pytest.raises(ValueError, match=r"workflow llm-pr-review is already owned by another agent \(thread agent-qg\)"):
        sync(connection, owner_thread_id=OTHER, path=theirs, text=forged)
    namespaces, workflows, _ = claims(connection)
    assert namespaces == [("qa", OWNER)] and workflows == [("llm-pr-review", OWNER)]  # the refusal rolled everything back
    assert REGISTRY.workflows == {"llm-pr-review": OWNER} and "qz.done" not in REGISTRY.events

    again = sync(connection, owner_thread_id=OWNER, path=mine, text=text)  # the real owner still syncs after the attempt
    assert {item["status"] for item in again["items"]} == {"unchanged"}


def test_the_workflow_value_must_be_the_name_of_the_skill_it_sits_in(tmp_path: Path) -> None:
    connection = db()
    text = small(deciding())
    path = skill(tmp_path, OWNER, "llm-pr-review", text, skill_name="something-else")
    with pytest.raises(ValueError, match=r"workflow llm-pr-review must be the name of the skill it sits in \(this SKILL.md is named something-else\)"):
        sync(connection, owner_thread_id=OWNER, path=path, text=text)
    assert claims(connection) == ([], [], 0)


def test_a_skill_that_names_another_agent_as_owner_is_refused_without_claiming(tmp_path: Path) -> None:
    connection = db()
    text = small(deciding())
    path = skill(tmp_path, OWNER, "llm-pr-review", text)
    with pytest.raises(ValueError, match="workflow llm-pr-review is owned by another agent; only its owner can sync it"):
        sync(connection, owner_thread_id=OTHER, path=path, text=text)
    assert claims(connection) == ([], [], 0)
