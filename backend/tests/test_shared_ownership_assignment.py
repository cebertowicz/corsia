from __future__ import annotations

import sqlite3
import sys
from pathlib import Path

import pytest

from infra.platform_paths import WorkspaceSelection
from tests.registry_helpers import make_session


@pytest.fixture
def assignment_scope(isolated_db, tmp_path, monkeypatch):
    from infra import config
    from infra.database import get_db
    from registry import file_resolution, service

    workspace = tmp_path / "workspace"
    root = workspace / "projects" / "alpha"
    shared = root / "shared"
    documents = shared / "documents"
    records = shared / "records"
    documents.mkdir(parents=True)
    records.mkdir()
    selection = WorkspaceSelection(
        workspace_id="workspace-stable-id",
        root=workspace.resolve(),
        name="Test workspace",
    )
    monkeypatch.setattr(config, "WORKSPACE", workspace)
    monkeypatch.setattr(config, "WORKSPACE_SELECTION", selection)
    monkeypatch.setattr(
        file_resolution, "get_allowed_dirs_rw", lambda **_kwargs: [workspace.resolve()],
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
    owner = make_session(
        db, project_id="project-alpha", title="Owner Lane", prefix="owner",
    )
    existing = make_session(
        db, project_id="project-alpha", title="Existing Lane", prefix="existing",
    )
    directory = make_session(
        db, project_id="project-alpha", title="Documents & Artifacts",
        prefix="directory",
    )
    conductor = make_session(
        db, project_id="project-alpha", title="Conductor", prefix="conductor",
    )
    temporary = make_session(
        db, project_id="project-alpha", title="Temporary", prefix="temporary",
    )
    outsider = make_session(
        db, project_id="project-alpha", title="Outsider", prefix="outsider",
    )
    db.execute(
        "UPDATE threads SET agent_enabled=1 WHERE id IN (?,?,?,?,?)",
        (owner, existing, directory, conductor, outsider),
    )
    db.execute("UPDATE threads SET spawned_by=? WHERE id=?", (owner, temporary))
    db.execute(
        "UPDATE projects SET conductor_thread_id=? WHERE id='project-alpha'",
        (conductor,),
    )
    (shared / "INDEX.md").write_text(
        "# Shared\n\n"
        f"- `documents/` — Documents & Artifacts ({directory})\n",
        encoding="utf-8",
    )
    (documents / "INDEX.md").write_text("# Documents\n", encoding="utf-8")
    (records / "INDEX.md").write_text("# Records\n", encoding="utf-8")
    db.commit()
    return {
        "workspace": workspace,
        "root": root,
        "shared": shared,
        "documents": documents,
        "records": records,
        "db": db,
        "owner": owner,
        "existing": existing,
        "directory": directory,
        "conductor": conductor,
        "temporary": temporary,
        "outsider": outsider,
    }


def _register(scope, path: Path, owner: str) -> str:
    from registry import service

    artifact = service.register_write_strict(owner, str(path), db=scope["db"])
    return str(artifact["id"])


def _counts(db) -> dict[str, int]:
    tables = (
        "artifact_identities",
        "artifact_registrations",
        "artifact_registration_events",
        "settings",
        "plans",
        "plan_nodes",
        "plan_node_artifact_links",
    )
    return {
        table: int(db.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0])
        for table in tables
    }


def _build_rule_tree(scope):
    status = (
        "**Status:** current · **Owner:** Owner Lane ({owner}) · "
        "**Verified at:** 2026-09-24 · **Review:** every merge\n"
    )
    header = scope["documents"] / "header.md"
    header.write_text(status.format(owner=scope["owner"]) + "body\n", encoding="utf-8")
    registry = scope["documents"] / "registry.md"
    registry.write_text("body\n", encoding="utf-8")
    acting = scope["documents"] / "acting.md"
    acting.write_text("body\n", encoding="utf-8")
    directory = scope["documents"] / "directory.md"
    directory.write_text("body\n", encoding="utf-8")
    fallback = scope["records"] / "fallback.md"
    fallback.write_text("body\n", encoding="utf-8")
    binary = scope["records"] / "image.png"
    binary.write_bytes(b"\x89PNG\r\n\x1a\nnot-text")
    ids = {
        "header": _register(scope, header, scope["existing"]),
        "registry": _register(scope, registry, scope["existing"]),
        "acting": _register(scope, acting, scope["temporary"]),
    }
    return {
        "header": header,
        "registry": registry,
        "acting": acting,
        "directory": directory,
        "fallback": fallback,
        "binary": binary,
        "ids": ids,
    }


def _projection(rows):
    return [
        (row.path, row.owner, row.rule, row.conflict, row.trigger, row.artifact_id)
        for row in rows
    ]


def test_inventory_exact_rows_and_dry_run_writes_nothing(assignment_scope):
    from registry.shared_ownership_assignment import inventory_shared_ownership

    scope = assignment_scope
    tree = _build_rule_tree(scope)
    before = _counts(scope["db"])
    rows = inventory_shared_ownership(
        scope["workspace"], "project-alpha", db=scope["db"],
    )
    assert _counts(scope["db"]) == before
    assert _projection(rows) == [
        (
            "shared/INDEX.md", scope["conductor"], "fallback", False, None, None,
        ),
        (
            "shared/documents/INDEX.md", scope["directory"], "directory", False,
            None, None,
        ),
        (
            "shared/documents/acting.md", scope["owner"], "acting_owner", False,
            None, tree["ids"]["acting"],
        ),
        (
            "shared/documents/directory.md", scope["directory"], "directory",
            False, None, None,
        ),
        (
            "shared/documents/header.md", scope["owner"], "header", True,
            "every merge", tree["ids"]["header"],
        ),
        (
            "shared/documents/registry.md", scope["existing"], "registry", False,
            None, tree["ids"]["registry"],
        ),
        (
            "shared/records/INDEX.md", scope["conductor"], "fallback", False,
            None, None,
        ),
        (
            "shared/records/fallback.md", scope["conductor"], "fallback", False,
            None, None,
        ),
        (
            "shared/records/image.png", scope["conductor"], "fallback", False,
            None, None,
        ),
    ]


def test_apply_is_atomic_idempotent_enforces_and_creates_one_claim_task(assignment_scope):
    from infra.settings import get_setting
    from plans import service as plan_service
    from registry.identity_metadata import parse_metadata
    from registry.shared_ownership_assignment import apply_shared_ownership
    from registry.shared_principals import artifact_owner

    scope = assignment_scope
    tree = _build_rule_tree(scope)
    rows = apply_shared_ownership(
        scope["workspace"], "project-alpha", apply=True, db=scope["db"],
    )
    assert len(rows) == 9
    assert get_setting("shared_ownership:project-alpha", db=scope["db"])[
        "shared_ownership_enforced_at"
    ]
    expected_owners = {
        "header": scope["owner"],
        "registry": scope["existing"],
        "acting": scope["owner"],
        "directory": scope["directory"],
        "fallback": scope["conductor"],
        "binary": scope["conductor"],
    }
    for name, owner in expected_owners.items():
        path = tree[name]
        artifact = __import__("registry.service", fromlist=["service"]).find_file_identity(
            str(path), db=scope["db"],
        )
        assert artifact_owner(str(artifact["id"]), db=scope["db"]) == owner
    header_artifact = __import__(
        "registry.service", fromlist=["service"],
    ).find_file_identity(str(tree["header"]), db=scope["db"])
    header_metadata = parse_metadata(header_artifact["metadata"])
    assert header_metadata["update_trigger"] == "every merge"
    assert header_metadata["last_verified_at"] == "2026-09-24T00:00:00+00:00"
    undated = __import__("registry.service", fromlist=["service"]).find_file_identity(
        str(tree["registry"]), db=scope["db"],
    )
    assert "last_verified_at" not in parse_metadata(undated["metadata"])

    recipients = {
        scope["owner"], scope["existing"], scope["directory"], scope["conductor"],
    }
    node_ids = []
    for recipient in recipients:
        plan = plan_service.ensure_plan("thread", recipient, db=scope["db"])
        nodes = scope["db"].execute(
            "SELECT id,title,description,acceptance_criteria FROM plan_nodes WHERE plan_id=?",
            (plan.id,),
        ).fetchall()
        assert len(nodes) == 1
        assert nodes[0]["title"] == "Claim your shared documents: keep, hand off, or archive"
        assert "remains active under a watch" in nodes[0]["acceptance_criteria"]
        description = nodes[0]["description"]
        assert (
            f"Paths are relative to the `project-alpha` scope root: `{scope['root']}`."
            in description
        )
        assert "neither triggers nor satisfies it" in description
        assert "make no content or ownership change and record keep" in description
        assert "resolve the linked input with artifact_resolve" in description
        assert "thread ID in agent_roster" in description
        assert "artifact_rehome with new_owner_thread_id" in description
        assert "Keep means retain the document in your responsibility" in description
        assert "Before any effect, read shared/standards/WORKSPACE-STANDARD.md" not in description
        assert "Do not archive without a scope standard" in description
        assert (
            "destination, status header, live and archive indexes, and living-reference preservation"
            in description
        )
        assert "ask the conductor to establish it" in description
        assert "plan_update accept with verification_result=passed" in description
        assert "plan_update arm_watch with a deadline" in description
        assert "block_node only when user action is required" in description
        assert "Never substitute chat for plan state" in description
        assert "When that watch fires, nothing happens automatically" in description
        assert "re-arm the deadline watch" in description
        assert "The user does not own the prerequisite" in description
        node_ids.append(str(nodes[0]["id"]))
    before = _counts(scope["db"])
    apply_shared_ownership(
        scope["workspace"], "project-alpha", apply=True, db=scope["db"],
    )
    assert _counts(scope["db"]) == before
    assert len(set(node_ids)) == 4


def test_startup_keeps_the_claim_for_a_preexisting_unenforced_scope(assignment_scope):
    from infra.settings import get_setting
    from registry.shared_ownership_assignment import run_startup_assignment

    scope = assignment_scope
    _build_rule_tree(scope)

    run_startup_assignment(scope["workspace"])

    assert get_setting("shared_ownership:project-alpha", db=scope["db"])
    assert scope["db"].execute(
        "SELECT 1 FROM plan_nodes WHERE title=?",
        ("Claim your shared documents: keep, hand off, or archive",),
    ).fetchone() is not None


def test_claim_instructions_reference_a_scoped_standard_when_present(assignment_scope):
    from registry.shared_ownership_assignment import apply_shared_ownership

    scope = assignment_scope
    _build_rule_tree(scope)
    standard = scope["shared"] / "standards" / "WORKSPACE-STANDARD.md"
    standard.parent.mkdir()
    standard.write_text("# Standard\n", encoding="utf-8")

    apply_shared_ownership(
        scope["workspace"], "project-alpha", apply=True, db=scope["db"],
    )

    description = scope["db"].execute(
        "SELECT n.description FROM plan_nodes n JOIN plans p ON p.id=n.plan_id "
        "WHERE p.owner_id=?",
        (scope["conductor"],),
    ).fetchone()["description"]
    assert "Before any effect, read shared/standards/WORKSPACE-STANDARD.md" in description


def test_apply_leaves_every_pane_as_it_was(assignment_scope):
    from registry.shared_ownership_assignment import apply_shared_ownership
    from registry.view import _artifacts_for_thread

    scope = assignment_scope
    _build_rule_tree(scope)
    threads = [
        scope[name]
        for name in ("owner", "existing", "directory", "conductor", "temporary", "outsider")
    ]

    def panes():
        return {
            thread: sorted(row["id"] for row in _artifacts_for_thread(scope["db"], thread))
            for thread in threads
        }

    before = panes()
    assert sum(len(rows) for rows in before.values()) == 3

    rows = apply_shared_ownership(
        scope["workspace"], "project-alpha", apply=True, db=scope["db"],
    )

    assert len(rows) == 9
    assert panes() == before

    # Working on an assigned document is what puts it in its owner's pane.
    worked_on = _register(scope, scope["documents"] / "directory.md", scope["directory"])
    assert panes() == {**before, scope["directory"]: [worked_on]}


def test_backfill_retires_only_untouched_rows_the_old_assignment_added(
    assignment_scope, monkeypatch,
):
    from infra.settings import get_setting
    from registry import node_retirement, ownership_row_backfill
    from registry.pinning import update_pinned
    from registry.shared_ownership_assignment import apply_shared_ownership
    from registry.view import _artifacts_for_thread

    scope = assignment_scope
    db = scope["db"]
    tree = _build_rule_tree(scope)
    threads = [scope[name] for name in ("owner", "existing", "directory", "conductor")]

    def panes():
        return {
            thread: sorted(row["id"] for row in _artifacts_for_thread(db, thread))
            for thread in threads
        }

    before = panes()
    # Reproduce the assignment as it ran before ownership stopped adding rows.
    with monkeypatch.context() as legacy:
        legacy.setattr(node_retirement, "retire_ownership_row", lambda *_a, **_k: 0)
        apply_shared_ownership(scope["workspace"], "project-alpha", apply=True, db=db)
    assert sum(map(len, panes().values())) == sum(map(len, before.values())) + 10

    find = __import__("registry.service", fromlist=["service"]).find_file_identity
    pinned = str(find(str(tree["header"]), db=db)["id"])
    touched = str(find(str(tree["directory"]), db=db)["id"])
    update_pinned(pinned, pinned=True, parent_thread_id=scope["owner"], db=db)
    db.execute(
        "UPDATE artifact_registrations SET updated_at='later' "
        "WHERE artifact_id=? AND parent_thread_id=?",
        (touched, scope["directory"]),
    )
    db.commit()

    retired = ownership_row_backfill.retire_assignment_rows(db)

    assert sum(retired.values()) == 8
    assert panes() == {
        **before,
        scope["owner"]: sorted([*before[scope["owner"]], pinned]),
        scope["directory"]: sorted([*before[scope["directory"]], touched]),
    }
    assert ownership_row_backfill.retire_assignment_rows(db) == {}

    ownership_row_backfill.run_startup_backfill()
    marker = get_setting("artifact_pane:ownership_assignment_rows_retired")
    assert marker["retired_rows"] == 0


def test_enforced_gate_uses_exact_n5a_refusal(assignment_scope):
    from registry.durable_writes import write_text
    from registry.shared_write_errors import DurableWritePolicyError
    from registry.shared_document_messages import owner_refusal
    from registry.shared_ownership_assignment import apply_shared_ownership

    scope = assignment_scope
    tree = _build_rule_tree(scope)
    apply_shared_ownership(
        scope["workspace"], "project-alpha", apply=True, db=scope["db"],
    )
    with pytest.raises(DurableWritePolicyError) as caught:
        write_text(
            tree["header"],
            "changed\n",
            thread_id=scope["outsider"],
            project_id="project-alpha",
            workspace=scope["workspace"],
            description="Changed",
            update_trigger="when facts change",
        )
    assert str(caught.value) == owner_refusal(
        tree["header"], scope["root"], scope["owner"], db=scope["db"],
    )


def test_header_resolves_scope_user_name_to_ui(assignment_scope):
    from registry.shared_ownership_assignment import inventory_shared_ownership

    scope = assignment_scope
    path = scope["records"] / "user-owned.md"
    path.write_text(
        "**Status:** current · **Owner:** Steve · **Review:** annually\n",
        encoding="utf-8",
    )
    row = next(
        item
        for item in inventory_shared_ownership(
            scope["workspace"], "project-alpha", db=scope["db"],
        )
        if item.path.endswith("user-owned.md")
    )
    assert (row.owner, row.rule, row.trigger) == ("ui", "header", "annually")


def test_unresolved_header_falls_through_to_next_rule_and_is_reported(assignment_scope):
    from infra.settings import get_setting
    from registry.shared_ownership_assignment import (
        apply_shared_ownership,
        inventory_shared_ownership,
    )

    scope = assignment_scope
    unresolved = scope["documents"] / "unresolved.md"
    unresolved.write_text(
        "**Status:** current · **Owner:** Missing Lane (missing-id) · "
        "**Review:** monthly\n",
        encoding="utf-8",
    )
    rows = inventory_shared_ownership(
        scope["workspace"], "project-alpha", db=scope["db"],
    )
    row = next(item for item in rows if item.path.endswith("unresolved.md"))
    assert (row.owner, row.rule, row.trigger, row.unresolved_header) == (
        scope["directory"], "directory", "monthly", "Missing Lane (missing-id)",
    )
    apply_shared_ownership(
        scope["workspace"], "project-alpha", apply=True, db=scope["db"],
    )
    assert get_setting("shared_ownership:project-alpha", db=scope["db"])
    description = scope["db"].execute(
        "SELECT n.description FROM plan_nodes n JOIN plans p ON p.id=n.plan_id "
        "WHERE p.owner_id=?",
        (scope["directory"],),
    ).fetchone()["description"]
    assert (
        "- `shared/documents/unresolved.md` — assigned by directory (its Owner header "
        "named no single agent: Missing Lane (missing-id))"
    ) in description


def test_temporary_owner_without_persistent_ancestor_falls_through(assignment_scope):
    from registry.shared_ownership_assignment import inventory_shared_ownership

    scope = assignment_scope
    orphan_owner = make_session(
        scope["db"], project_id="project-alpha", title="Chat", prefix="chat",
    )
    registered = scope["records"] / "chat-written.md"
    registered.write_text("body\n", encoding="utf-8")
    artifact_id = _register(scope, registered, orphan_owner)
    row = next(
        item
        for item in inventory_shared_ownership(
            scope["workspace"], "project-alpha", db=scope["db"],
        )
        if item.path.endswith("chat-written.md")
    )
    assert (row.owner, row.rule, row.artifact_id) == (
        scope["conductor"], "fallback", artifact_id,
    )


def test_file_without_any_owner_leaves_flag_unset_and_writes_nothing(assignment_scope):
    from infra.settings import get_setting
    from registry.shared_ownership_assignment import apply_shared_ownership

    scope = assignment_scope
    (scope["records"] / "orphan.md").write_text("body\n", encoding="utf-8")
    scope["db"].execute(
        "UPDATE projects SET conductor_thread_id=NULL WHERE id='project-alpha'",
    )
    scope["db"].commit()
    before = _counts(scope["db"])
    with pytest.raises(ValueError, match="orphan.md.*fallback"):
        apply_shared_ownership(
            scope["workspace"], "project-alpha", apply=True, db=scope["db"],
        )
    assert _counts(scope["db"]) == before
    assert get_setting("shared_ownership:project-alpha", db=scope["db"]) is None


def test_assignment_and_flag_roll_back_together(assignment_scope, monkeypatch):
    from registry import shared_ownership_assignment as assignment

    scope = assignment_scope
    tree = _build_rule_tree(scope)
    before = _counts(scope["db"])
    original_owner = scope["existing"]

    def fail_setting(*_args, **_kwargs):
        raise RuntimeError("setting failed")

    monkeypatch.setattr(assignment, "set_setting", fail_setting)
    with pytest.raises(RuntimeError, match="setting failed"):
        assignment.apply_shared_ownership(
            scope["workspace"], "project-alpha", apply=True, db=scope["db"],
        )
    assert _counts(scope["db"]) == before
    artifact = __import__("registry.service", fromlist=["service"]).find_file_identity(
        str(tree["header"]), db=scope["db"],
    )
    from registry.identity_metadata import parse_metadata
    from registry.shared_principals import artifact_owner

    assert artifact_owner(str(artifact["id"]), db=scope["db"]) == original_owner
    assert "last_verified_at" not in parse_metadata(artifact["metadata"])


def test_cli_dry_run_opens_database_read_only_without_touching_bytes_or_mtime(
    assignment_scope, tmp_path, monkeypatch, capsys,
):
    from registry import shared_ownership_assignment as assignment

    scope = assignment_scope
    _build_rule_tree(scope)
    scope["db"].commit()
    snapshot = tmp_path / "registry.sqlite"
    copied = sqlite3.connect(snapshot)
    scope["db"].backup(copied)
    copied.close()
    before_bytes = snapshot.read_bytes()
    before_mtime = snapshot.stat().st_mtime_ns
    monkeypatch.setattr(
        sys,
        "argv",
        [
            "shared_ownership_assignment",
            "--dry-run",
            "--workspace",
            str(scope["workspace"]),
            "--db",
            str(snapshot),
            "--project-id",
            "project-alpha",
        ],
    )
    assert assignment.main() == 0
    output = capsys.readouterr().out
    assert "Conductor fallback:" in output
    assert "| shared/documents/header.md |" in output
    assert snapshot.read_bytes() == before_bytes
    assert snapshot.stat().st_mtime_ns == before_mtime


def test_cli_rejects_database_without_registry_schema(tmp_path):
    from registry.shared_ownership_assignment import _open_readonly_database

    path = tmp_path / "empty.sqlite"
    sqlite3.connect(path).close()
    with pytest.raises(SystemExit, match="missing the CORSIA registry schema"):
        _open_readonly_database(path)


def test_shared_index_directory_parser_reads_parenthesized_thread_id():
    from registry.shared_category_index import directory_owner_threads

    assert directory_owner_threads(
        "- `documents/` — Documents & Artifacts (6362be75)\n"
    ) == {"documents": "6362be75"}


def test_startup_assignment_closes_its_database_connection(tmp_path, monkeypatch):
    from registry import shared_ownership_assignment as assignment

    class StartupDb:
        closed = False

        def execute(self, sql):
            assert sql == "SELECT id FROM projects ORDER BY id"
            return ()

        def close(self):
            self.closed = True

    db = StartupDb()
    calls = []
    monkeypatch.setattr(assignment, "get_db", lambda: db)
    monkeypatch.setattr(assignment, "conductor_adopts_unowned", lambda *_a, **_k: False)
    monkeypatch.setattr(
        assignment,
        "apply_shared_ownership",
        lambda workspace, project_id, *, apply, db: calls.append(
            (workspace, project_id, apply, db)
        ),
    )
    assignment.run_startup_assignment(tmp_path)
    assert calls == [(tmp_path, None, True, db)]
    assert db.closed is True


def test_startup_assignment_attempts_later_scopes_after_one_scope_fails(
    tmp_path, monkeypatch,
):
    from registry import shared_ownership_assignment as assignment

    class StartupDb:
        closed = False

        def execute(self, sql):
            return ({"id": "p1"}, {"id": "p2"}, {"id": "p3"})

        def close(self):
            self.closed = True

    db = StartupDb()
    attempted = []

    def enforced(scope_id, *, db):
        if scope_id == "p1":
            raise RuntimeError("enforcement read failed")
        return False

    def apply(workspace, project_id, *, apply, db):
        attempted.append(project_id)
        if project_id == "p2":
            raise RuntimeError("apply failed")

    monkeypatch.setattr(assignment, "get_db", lambda: db)
    monkeypatch.setattr(assignment, "conductor_adopts_unowned", enforced)
    monkeypatch.setattr(assignment, "apply_shared_ownership", apply)
    assignment.run_startup_assignment(tmp_path)
    # p1's enforcement read and p2's apply both fail; p3 is still attempted.
    assert attempted == [None, "p2", "p3"]
    assert db.closed is True
