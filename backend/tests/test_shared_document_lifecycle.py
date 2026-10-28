from __future__ import annotations

from pathlib import Path

import pytest

from infra.platform_paths import WorkspaceSelection
from tests.registry_helpers import make_session


@pytest.fixture
def lifecycle_scope(isolated_db, tmp_path, monkeypatch):
    from infra import config
    from infra.database import get_db
    from registry import service

    workspace = tmp_path / "workspace"
    project_root = workspace / "projects" / "alpha"
    category = project_root / "shared" / "documents"
    category.mkdir(parents=True)
    (project_root / "shared" / "INDEX.md").write_text("# Shared\n", encoding="utf-8")
    (category / "INDEX.md").write_text("# Documents\n", encoding="utf-8")
    selection = WorkspaceSelection(
        workspace_id="workspace-stable-id",
        root=workspace.resolve(),
        name="Test workspace",
    )
    monkeypatch.setattr(config, "WORKSPACE", workspace)
    monkeypatch.setattr(config, "WORKSPACE_SELECTION", selection)
    monkeypatch.setattr(
        service,
        "get_allowed_dirs_rw",
        lambda **_kwargs: [workspace.resolve()],
    )
    db = get_db()
    db.execute(
        "INSERT INTO projects (id,name,path,status,created_at,updated_at) "
        "VALUES (?,?,?,?,?,?)",
        (
            "project-alpha",
            "Alpha",
            "workspace://workspace-stable-id/projects/alpha",
            "active",
            "t",
            "t",
        ),
    )
    parent = make_session(db, project_id="project-alpha", title="Parent", prefix="parent")
    worker = make_session(db, project_id="project-alpha", title="Worker", prefix="worker")
    child = make_session(db, project_id="project-alpha", title="Child", prefix="child")
    agent = make_session(db, project_id="project-alpha", title="Agent", prefix="agent")
    conductor = make_session(db, project_id="project-alpha", title="Conductor", prefix="conductor")
    db.execute(
        "UPDATE threads SET agent_enabled=1 WHERE id IN (?,?,?)",
        (parent, agent, conductor),
    )
    db.execute("UPDATE threads SET spawned_by=? WHERE id=?", (parent, worker))
    db.execute("UPDATE threads SET spawned_by=? WHERE id=?", (worker, child))
    db.execute(
        "UPDATE projects SET conductor_thread_id=? WHERE id='project-alpha'",
        (conductor,),
    )
    db.commit()
    return {
        "workspace": workspace,
        "root": project_root,
        "category": category,
        "db": db,
        "parent": parent,
        "worker": worker,
        "child": child,
        "agent": agent,
        "conductor": conductor,
    }


def _register_file(scope, owner: str, path: Path, *, retention: str = "durable"):
    from registry import service

    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("body\n", encoding="utf-8")
    return service.register_write_strict(
        owner,
        str(path),
        retention=retention,
        db=scope["db"],
    )


def _register_pr(scope, owner: str, suffix: str):
    from registry import models

    return models.register_artifact(
        integration="github",
        type="pull_request",
        external_id=f"org/repo#{suffix}",
        parent_thread_id=owner,
        title=f"PR {suffix}",
        retention="durable",
        db=scope["db"],
    )


def _owner(db, artifact_id: str) -> str | None:
    row = db.execute(
        "SELECT parent_thread_id FROM artifact_registrations "
        "WHERE artifact_id=? AND relationship_state LIKE 'owner-%'",
        (artifact_id,),
    ).fetchone()
    return str(row["parent_thread_id"]) if row else None


def _rehome_events(db, artifact_id: str) -> int:
    return int(
        db.execute(
            "SELECT COUNT(*) FROM artifact_registration_events "
            "WHERE artifact_id=? AND event_type='ownership_rehomed'",
            (artifact_id,),
        ).fetchone()[0]
    )


def test_thread_end_routes_shared_pr_and_private_artifacts_once(lifecycle_scope):
    from registry.identity_bridge import PARENT_CHAIN_REGISTRATION_STATE
    from registry.ownership import rehome_on_thread_end

    scope = lifecycle_scope
    db = scope["db"]
    worker = scope["worker"]
    parent = scope["parent"]
    shared = _register_file(
        scope,
        worker,
        scope["category"] / "worker-owned.md",
    )
    pr = _register_pr(scope, worker, "401")
    private = _register_file(
        scope,
        worker,
        scope["root"] / ".threads" / worker / "private.md",
    )
    db.execute(
        "INSERT INTO artifact_registrations "
        "(id,artifact_id,parent_thread_id,relationship_state,tmp,surfacing,created_at,updated_at) "
        "VALUES (?,?,?,?,0,'quiet','t','t')",
        ("parent-chain", shared["id"], parent, PARENT_CHAIN_REGISTRATION_STATE),
    )
    db.commit()

    moved = rehome_on_thread_end(db, worker, "archived")

    assert len(moved) == 2
    assert _owner(db, shared["id"]) == parent
    assert _owner(db, pr["id"]) == parent
    assert _owner(db, private["id"]) == worker
    assert _rehome_events(db, shared["id"]) == 1
    assert _rehome_events(db, pr["id"]) == 1
    assert db.execute(
        "SELECT COUNT(*) FROM artifact_registrations WHERE artifact_id=? "
        "AND parent_thread_id=? AND relationship_state=?",
        (shared["id"], parent, PARENT_CHAIN_REGISTRATION_STATE),
    ).fetchone()[0] == 0


def test_persistent_agent_shared_documents_go_to_conductor(lifecycle_scope):
    from registry.ownership import rehome_on_thread_end

    scope = lifecycle_scope
    artifact = _register_file(
        scope,
        scope["agent"],
        scope["category"] / "agent-owned.md",
    )

    rehome_on_thread_end(scope["db"], scope["agent"], "agent_disabled")

    assert _owner(scope["db"], artifact["id"]) == scope["conductor"]
    assert _rehome_events(scope["db"], artifact["id"]) == 1


@pytest.mark.parametrize("who", ["agent", "worker"])
@pytest.mark.parametrize("integration,kind", [("claude-code", "session"), ("codex", "thread")])
def test_disabled_agent_keeps_its_provider_session(lifecycle_scope, who, integration, kind):
    from registry import models
    from registry.listing import find_for_thread
    from registry.ownership import rehome_on_thread_end

    scope = lifecycle_scope
    thread = scope[who]
    models.register_artifact(integration=integration, type=kind, external_id=f"{who}-own",
                             parent_thread_id=thread, db=scope["db"])

    rehome_on_thread_end(scope["db"], thread, "agent_disabled")

    # The thread stays live as a chat, so it still resumes its own session.
    record = find_for_thread(thread, integration, kind, db=scope["db"])
    assert record is not None and record["external_id"] == f"{who}-own"
    assert find_for_thread(scope["parent"], integration, kind, db=scope["db"]) is None


@pytest.mark.parametrize("reason", ["archived", "purged", "deleted"])
def test_family_ending_set_skips_intermediate_parent(lifecycle_scope, reason):
    from registry.ownership import rehome_on_thread_end

    scope = lifecycle_scope
    artifact = _register_file(
        scope,
        scope["child"],
        scope["category"] / f"family-{reason}.md",
    )
    ending = {scope["child"], scope["worker"], scope["parent"]}

    rehome_on_thread_end(
        scope["db"],
        scope["child"],
        reason,
        ending_thread_ids=ending,
    )

    assert _owner(scope["db"], artifact["id"]) == scope["conductor"]
    assert _rehome_events(scope["db"], artifact["id"]) == 1


def test_archive_writer_moves_only_on_changed_row(lifecycle_scope):
    from core.thread_lifecycle import set_user_archive_state
    from infra.database import write_transaction

    scope = lifecycle_scope
    artifact = _register_pr(scope, scope["worker"], "402")

    with write_transaction(scope["db"], scope="first_archive") as connection:
        changed = set_user_archive_state(
            connection, scope["worker"], archived=True, now="t2"
        )
    assert changed
    first_events = _rehome_events(scope["db"], artifact["id"])
    assert _owner(scope["db"], artifact["id"]) == scope["parent"]
    with write_transaction(scope["db"], scope="repeat_archive") as connection:
        changed_again = set_user_archive_state(
            connection, scope["worker"], archived=True, now="t3"
        )
    assert not changed_again
    assert _rehome_events(scope["db"], artifact["id"]) == first_events == 1


@pytest.mark.parametrize("writer", ["user", "settled", "dormancy"])
def test_each_archive_writer_rehomes_shared_document(lifecycle_scope, writer):
    from datetime import datetime, timezone

    from core.thread_lifecycle import (
        _apply_archive,
        archive_settled_worker,
        set_user_archive_state,
    )
    from infra.database import write_transaction

    scope = lifecycle_scope
    artifact = _register_file(
        scope,
        scope["worker"],
        scope["category"] / f"archive-{writer}.md",
    )
    with write_transaction(scope["db"], scope=f"archive_{writer}") as connection:
        if writer == "user":
            changed = set_user_archive_state(
                connection, scope["worker"], archived=True, now="2026-09-29T00:00:00Z"
            )
            assert changed
        elif writer == "settled":
            assert archive_settled_worker(
                connection, scope["worker"], now="2026-09-29T00:00:00Z"
            )
        else:
            _apply_archive(
                connection,
                scope["worker"],
                datetime(2026, 9, 29, tzinfo=timezone.utc),
                "dormancy",
            )

    assert _owner(scope["db"], artifact["id"]) == scope["parent"]
    assert _rehome_events(scope["db"], artifact["id"]) == 1


def test_purge_writer_rehomes_shared_document(lifecycle_scope):
    from datetime import datetime, timezone

    from core.thread_lifecycle import _apply_purge
    from infra.database import write_transaction

    scope = lifecycle_scope
    artifact = _register_file(
        scope,
        scope["worker"],
        scope["category"] / "purged.md",
    )
    with write_transaction(scope["db"], scope="purge_shared_owner") as connection:
        _apply_purge(
            connection,
            scope["worker"],
            datetime(2026, 9, 29, tzinfo=timezone.utc),
        )

    assert _owner(scope["db"], artifact["id"]) == scope["parent"]
    assert _rehome_events(scope["db"], artifact["id"]) == 1


def test_family_delete_writer_rehomes_once_to_final_live_owner(lifecycle_scope):
    from core.thread_family import delete_thread_family, thread_family_ids
    from infra.database import write_transaction

    scope = lifecycle_scope
    artifact = _register_file(
        scope,
        scope["child"],
        scope["category"] / "family-delete.md",
    )
    with write_transaction(scope["db"], scope="delete_shared_family") as connection:
        family = thread_family_ids(connection, scope["worker"])
        delete_thread_family(connection, family)

    assert _owner(scope["db"], artifact["id"]) == scope["parent"]
    assert _rehome_events(scope["db"], artifact["id"]) == 1
    assert scope["db"].execute(
        "SELECT COUNT(*) FROM threads WHERE id IN (?,?)",
        (scope["worker"], scope["child"]),
    ).fetchone()[0] == 0


@pytest.mark.parametrize("owner_key", ["agent", "worker"])
def test_thread_end_leaves_an_archived_artifact_where_it_is(lifecycle_scope, owner_key):
    from core.thread_family import delete_thread_family, thread_family_ids
    from infra.database import write_transaction

    scope = lifecycle_scope
    db = scope["db"]
    owner = scope[owner_key]
    artifact = _register_pr(scope, owner, f"archived-{owner_key}")
    db.execute("UPDATE artifact_identities SET state='archived' WHERE id=?", (artifact["id"],))
    db.commit()

    with write_transaction(db, scope="delete_archived_owner") as connection:
        delete_thread_family(connection, thread_family_ids(connection, owner))

    identity = db.execute(
        "SELECT state FROM artifact_identities WHERE id=?", (artifact["id"],)
    ).fetchone()
    assert identity["state"] == "archived"
    assert _owner(db, artifact["id"]) is None
    assert _rehome_events(db, artifact["id"]) == 0
    assert db.execute("SELECT COUNT(*) FROM threads WHERE id=?", (owner,)).fetchone()[0] == 0


def test_disable_writer_uses_user_when_no_live_conductor(lifecycle_scope):
    from core.agent_roster_mutations import change_agent
    from registry.shared_principals import UI_PRINCIPAL

    scope = lifecycle_scope
    artifact = _register_file(
        scope,
        scope["agent"],
        scope["category"] / "disabled-no-conductor.md",
    )
    row = scope["db"].execute(
        "SELECT user_id FROM threads WHERE id=?", (scope["agent"],)
    ).fetchone()
    scope["db"].execute(
        "UPDATE projects SET conductor_thread_id=NULL WHERE id='project-alpha'"
    )
    scope["db"].commit()

    result = change_agent(
        scope["agent"],
        {"agent_enabled": False},
        user_id=str(row["user_id"]),
    )

    assert result["changed"] is True
    assert _owner(scope["db"], artifact["id"]) == UI_PRINCIPAL
    assert _rehome_events(scope["db"], artifact["id"]) == 1
    repeated = change_agent(
        scope["agent"],
        {"agent_enabled": False},
        user_id=str(row["user_id"]),
    )
    assert repeated["changed"] is False
    assert _rehome_events(scope["db"], artifact["id"]) == 1


def test_archiving_conductor_rehomes_its_shared_document_to_user(lifecycle_scope):
    from core.thread_lifecycle import set_user_archive_state
    from infra.database import write_transaction
    from registry.shared_principals import UI_PRINCIPAL

    scope = lifecycle_scope
    artifact = _register_file(
        scope,
        scope["conductor"],
        scope["category"] / "conductor-ended.md",
    )
    with write_transaction(scope["db"], scope="archive_conductor") as connection:
        assert set_user_archive_state(
            connection,
            scope["conductor"],
            archived=True,
            now="2026-09-29T00:00:00Z",
        )

    assert _owner(scope["db"], artifact["id"]) == UI_PRINCIPAL
    assert _rehome_events(scope["db"], artifact["id"]) == 1


def test_archive_rehome_failure_rolls_back_transition(lifecycle_scope, monkeypatch):
    from core.thread_lifecycle import set_user_archive_state
    from infra.database import write_transaction
    from registry import ownership

    scope = lifecycle_scope

    def fail(*_args, **_kwargs):
        raise RuntimeError("rehome failed")

    monkeypatch.setattr(ownership, "rehome_on_thread_end", fail)
    with pytest.raises(RuntimeError, match="rehome failed"):
        with write_transaction(scope["db"], scope="archive_with_rehome") as connection:
            set_user_archive_state(
                connection,
                scope["worker"],
                archived=True,
                now="t2",
            )

    state = scope["db"].execute(
        "SELECT lifecycle_state FROM threads WHERE id=?",
        (scope["worker"],),
    ).fetchone()["lifecycle_state"]
    assert state in (None, "active")
