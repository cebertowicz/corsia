"""Phase 3a — thread directory restructure: migration + classification tests."""

from __future__ import annotations

import json
import sqlite3

import pytest

from tests.project_ref_helpers import project_ref, select_workspace


def _write(root, rel: str, text: str = "x") -> None:
    p = root / rel
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(text, encoding="utf-8")


def test_migrate_moves_working_layout_directly_to_hidden_thread_dir(tmp_path):
    from core.thread_dir_migration import migrate_scope

    _write(tmp_path, "working/sess/skills/deploy/SKILL.md", "skill")
    _write(tmp_path, "working/sess/notes.md", "scratch note")
    _write(tmp_path, "working/sess/sub/data.txt", "nested scratch")

    result = migrate_scope(tmp_path, thread_ids={"sess"})

    assert (tmp_path / ".threads" / "sess" / "skills" / "deploy" / "SKILL.md").read_text() == "skill"
    assert (tmp_path / ".threads" / "sess" / "working" / "notes.md").read_text() == "scratch note"
    assert (tmp_path / ".threads" / "sess" / "working" / "sub" / "data.txt").read_text() == "nested scratch"
    assert not (tmp_path / "working" / "sess").exists()
    assert not (tmp_path / "threads").exists()
    assert result.moved_skills == 1
    assert result.moved_working == 2
    assert result.threads_migrated == ["sess"]


def test_migrate_is_idempotent_noop_on_second_run(tmp_path):
    from core.thread_dir_migration import migrate_scope

    _write(tmp_path, "working/sess/skills/deploy/SKILL.md", "skill")
    _write(tmp_path, "working/sess/notes.md", "note")

    first = migrate_scope(tmp_path, thread_ids={"sess"})
    assert first.threads_migrated == ["sess"]

    second = migrate_scope(tmp_path, thread_ids={"sess"})
    assert second.moved_skills == 0
    assert second.moved_working == 0
    assert second.threads_migrated == []
    # Content intact after the no-op run.
    assert (tmp_path / ".threads" / "sess" / "skills" / "deploy" / "SKILL.md").read_text() == "skill"


def test_migrate_prod_safe_leaves_loose_files_and_unknown_dirs(tmp_path):
    from core.thread_dir_migration import migrate_scope

    _write(tmp_path, "working/loose-plan.md", "project scratch")           # loose file
    _write(tmp_path, "working/2026-planning/audit.md", "planning dir")     # not a thread id
    _write(tmp_path, "working/sess/notes.md", "thread scratch")            # a real thread

    result = migrate_scope(tmp_path, thread_ids={"sess"})

    assert (tmp_path / "working" / "loose-plan.md").exists()
    assert (tmp_path / "working" / "2026-planning" / "audit.md").exists()
    assert not (tmp_path / ".threads" / "2026-planning").exists()
    assert (tmp_path / ".threads" / "sess" / "working" / "notes.md").exists()
    assert result.threads_migrated == ["sess"]


def test_migrate_refuses_destination_collision_without_partial_moves(tmp_path):
    from core.thread_dir_migration import migrate_scope

    _write(tmp_path, "working/first/notes.md", "must stay")
    _write(tmp_path, "working/sess/skills/deploy/SKILL.md", "old")
    _write(tmp_path, ".threads/sess/skills/deploy/SKILL.md", "already-migrated")

    with pytest.raises(ValueError, match="collision"):
        migrate_scope(tmp_path, thread_ids={"first", "sess"})

    assert (tmp_path / ".threads" / "sess" / "skills" / "deploy" / "SKILL.md").read_text() == "already-migrated"
    assert (tmp_path / "working" / "sess" / "skills" / "deploy" / "SKILL.md").read_text() == "old"
    assert (tmp_path / "working" / "first" / "notes.md").read_text() == "must stay"
    assert not (tmp_path / ".threads" / "first").exists()


def test_migrate_refuses_both_legacy_layouts_for_one_thread(tmp_path):
    from core.thread_dir_migration import migrate_scope

    _write(tmp_path, "working/sess/notes.md", "working source")
    _write(tmp_path, "threads/sess/context/standing.md", "structured source")

    with pytest.raises(ValueError, match="collision"):
        migrate_scope(tmp_path, thread_ids={"sess"})

    assert (tmp_path / "working/sess/notes.md").read_text() == "working source"
    assert (tmp_path / "threads/sess/context/standing.md").read_text() == "structured source"
    assert not (tmp_path / ".threads/sess").exists()


def test_migrate_moves_structured_legacy_thread_without_reformatting(tmp_path):
    from core.thread_dir_migration import migrate_scope

    _write(tmp_path, "threads/sess/skills/review/SKILL.md", "durable skill")
    _write(tmp_path, "threads/sess/context/standing.md", "durable context")
    _write(tmp_path, "threads/sess/working/note.md", "scratch")

    result = migrate_scope(tmp_path, thread_ids={"sess"})

    assert (tmp_path / ".threads/sess/skills/review/SKILL.md").read_text() == "durable skill"
    assert (tmp_path / ".threads/sess/context/standing.md").read_text() == "durable context"
    assert (tmp_path / ".threads/sess/working/note.md").read_text() == "scratch"
    assert not (tmp_path / "threads/sess").exists()
    assert result.threads_migrated == ["sess"]


def test_migrate_rejects_symlinked_legacy_source_or_destination(tmp_path):
    from core.thread_dir_migration import migrate_scope

    outside = tmp_path / "outside"
    outside.mkdir()
    (tmp_path / "working").mkdir()
    (tmp_path / "working/sess").symlink_to(outside, target_is_directory=True)

    with pytest.raises(ValueError, match="symlink"):
        migrate_scope(tmp_path, thread_ids={"sess"})

    (tmp_path / "working/sess").unlink()
    _write(tmp_path, "working/sess/notes.md", "source")
    (tmp_path / ".threads").mkdir()
    (tmp_path / ".threads/sess").symlink_to(outside, target_is_directory=True)
    with pytest.raises(ValueError, match="symlink"):
        migrate_scope(tmp_path, thread_ids={"sess"})
    assert (tmp_path / "working/sess/notes.md").read_text() == "source"


def test_migrate_requires_db_known_thread_ids(tmp_path):
    from core.thread_dir_migration import migrate_scope

    _write(tmp_path, "working/sess/notes.md", "source")

    with pytest.raises(ValueError, match="thread_ids"):
        migrate_scope(tmp_path)
    assert (tmp_path / "working/sess/notes.md").read_text() == "source"


def test_migrate_dry_run_moves_nothing(tmp_path):
    from core.thread_dir_migration import migrate_scope

    _write(tmp_path, "working/sess/notes.md", "note")
    result = migrate_scope(tmp_path, thread_ids={"sess"}, dry_run=True)

    assert (tmp_path / "working" / "sess" / "notes.md").exists()
    assert not (tmp_path / ".threads" / "sess").exists()
    assert result.moved_working == 1  # reports the plan


def test_migrate_no_working_dir_is_noop(tmp_path):
    from core.thread_dir_migration import migrate_scope

    result = migrate_scope(tmp_path, thread_ids={"sess"})
    assert result.threads_migrated == []
    assert result.moved_skills == result.moved_working == 0


def test_new_layout_tmp_classification(tmp_path):
    """.threads/{id}/working is ephemeral; skills/ and context/ are durable."""
    from registry.service import is_tmp

    assert is_tmp(tmp_path / ".threads" / "s" / "working" / "note.md") is True
    assert is_tmp(tmp_path / ".threads" / "s" / "skills" / "x" / "SKILL.md") is False
    assert is_tmp(tmp_path / ".threads" / "s" / "context" / "standing.md") is False


def test_configured_bootloader_file_routes_to_context_not_working(tmp_path):
    """A configured (durable) thread bootloader file under working/{id} lands in
    .threads/{id}/context (durable), not .threads/{id}/working (ephemeral)."""
    from core.thread_dir_migration import migrate_scope

    _write(tmp_path, "working/sess/standing.md", "durable config")
    _write(tmp_path, "working/sess/scratch.md", "ephemeral note")

    result = migrate_scope(
        tmp_path, thread_ids={"sess"}, durable_by_thread={"sess": {"standing.md"}}
    )

    assert (tmp_path / ".threads" / "sess" / "context" / "standing.md").read_text() == "durable config"
    assert (tmp_path / ".threads" / "sess" / "working" / "scratch.md").read_text() == "ephemeral note"
    assert not (tmp_path / ".threads" / "sess" / "working" / "standing.md").exists()
    assert result.moved_context == 1
    assert result.moved_working == 1


def test_nested_configured_path_move_and_repoint_agree(tmp_path):
    """A configured bootloader file nested under working/{id}/sub/ is durable →
    routed to .threads/{id}/context/sub/ (subpath preserved) by BOTH the physical
    move and the DB repoint (no orphaned reference, no durability loss)."""
    from core.thread_dir_migration import (
        _repoint_entry,
        migrate_scope,
        session_working_relpaths,
    )

    entries = ["/ws/working/sess/sub/foo.md"]
    durable = session_working_relpaths(entries, "sess")
    assert durable == {"sub/foo.md"}  # configured => durable at any depth

    _write(tmp_path, "working/sess/sub/foo.md", "nested config")
    _write(tmp_path, "working/sess/sub/scratch.md", "sibling scratch")
    migrate_scope(tmp_path, thread_ids={"sess"}, durable_by_thread={"sess": durable})

    # Durable nested config → context/ (subpath preserved); sibling scratch → working/.
    assert (tmp_path / ".threads" / "sess" / "context" / "sub" / "foo.md").read_text() == "nested config"
    assert (tmp_path / ".threads" / "sess" / "working" / "sub" / "scratch.md").read_text() == "sibling scratch"
    assert not (tmp_path / ".threads" / "sess" / "working" / "sub" / "foo.md").exists()
    # Repoint agrees: same context/ subpath.
    assert _repoint_entry("/ws/working/sess/sub/foo.md", "sess") == "/ws/.threads/sess/context/sub/foo.md"


def test_skills_prefixed_configured_move_and_repoint_agree(tmp_path):
    """A configured entry under working/{id}/skills/ takes skills precedence in
    BOTH the move and the repoint (shared router) — no context/skills divergence."""
    from core.thread_dir_migration import _repoint_entry, migrate_scope, session_working_relpaths

    entries = ["/ws/working/sess/skills/foo.md"]
    durable = session_working_relpaths(entries, "sess")
    assert durable == {"skills/foo.md"}

    _write(tmp_path, "working/sess/skills/foo.md", "cfg under skills")
    migrate_scope(tmp_path, thread_ids={"sess"}, durable_by_thread={"sess": durable})

    # Physical: skills precedence -> .threads/{id}/skills/foo.md (NOT context/skills/).
    assert (tmp_path / ".threads" / "sess" / "skills" / "foo.md").read_text() == "cfg under skills"
    assert not (tmp_path / ".threads" / "sess" / "context" / "skills" / "foo.md").exists()
    # Repoint agrees via the shared router.
    assert _repoint_entry("/ws/working/sess/skills/foo.md", "sess") == "/ws/.threads/sess/skills/foo.md"


def test_target_from_body_thread_root_workspace_and_project(monkeypatch, tmp_path):
    """_target_from_body resolves the thread bootloader root to
    .threads/{id}/context under the workspace (no project) and under the project
    planning dir (project-scoped) — the same dir the dir-scan reader uses."""
    import api.context_bootloader as cb
    from infra import config

    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    conn.execute("CREATE TABLE projects (id TEXT PRIMARY KEY, name TEXT, path TEXT)")
    workspace = tmp_path / "workspace"
    project = workspace / "projects" / "corsia"
    project.mkdir(parents=True)
    monkeypatch.setattr(cb.config, "WORKSPACE", workspace)
    select_workspace(monkeypatch, workspace)
    conn.execute("INSERT INTO projects (id, name, path) VALUES ('p1', 'CORSIA', ?)", (project_ref("corsia"),))

    # Workspace-scoped thread (no project).
    monkeypatch.setattr(cb, "get_thread_for_user", lambda sid, uid, db=None: {"project_id": None})
    ws_target = cb._target_from_body({"scope": "thread", "thread_id": "sw"}, conn, "u")
    assert ws_target.root == config.WORKSPACE / ".threads" / "sw" / "context"

    # Project-scoped thread → under the project planning dir (projects/corsia).
    monkeypatch.setattr(cb, "get_thread_for_user", lambda sid, uid, db=None: {"project_id": "p1"})
    pj_target = cb._target_from_body({"scope": "thread", "thread_id": "sp"}, conn, "u")
    assert pj_target.root == project / ".threads" / "sp" / "context"
    conn.close()


def test_target_from_body_rejects_unresolved_project_thread(monkeypatch, tmp_path):
    from fastapi import HTTPException
    import api.context_bootloader as cb

    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    conn.execute("CREATE TABLE projects (id TEXT PRIMARY KEY, name TEXT, path TEXT)")
    conn.execute("INSERT INTO projects (id, name) VALUES ('p1', 'Missing')")
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    monkeypatch.setattr(cb.config, "WORKSPACE", workspace)
    monkeypatch.setattr(cb, "get_thread_for_user", lambda sid, uid, db=None: {"project_id": "p1"})

    with pytest.raises(HTTPException, match="unresolved"):
        cb._target_from_body({"scope": "thread", "thread_id": "sp"}, conn, "u")
    conn.close()


def test_repoint_session_bootloader_paths_targets_context(tmp_path):
    from core.thread_dir_migration import repoint_session_bootloader_paths

    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    conn.execute("CREATE TABLE threads (id TEXT PRIMARY KEY, bootloader_files TEXT)")
    conn.execute(
        "INSERT INTO threads (id, bootloader_files) VALUES (?, ?)",
        (
            "sess",
            json.dumps(
                [
                    "/ws/working/sess/standing.md",
                    "/ws/threads/sess/context/legacy.md",
                    "/ws/.threads/sess/context/current.md",
                    "/ws/other.md",
                ]
            ),
        ),
    )
    # A different session's path must not be rewritten under this row.
    conn.execute(
        "INSERT INTO threads (id, bootloader_files) VALUES (?, ?)",
        ("sess2", json.dumps(["/ws/working/sess/foreign.md"])),
    )

    rewritten = repoint_session_bootloader_paths(conn)
    assert rewritten == 1

    row = conn.execute("SELECT bootloader_files FROM threads WHERE id='sess'").fetchone()
    # Durable config repointed to context/ (matches the physical move); the
    # out-of-thread entry passes through untouched.
    assert json.loads(row["bootloader_files"]) == [
        "/ws/.threads/sess/context/standing.md",
        "/ws/.threads/sess/context/legacy.md",
        "/ws/.threads/sess/context/current.md",
        "/ws/other.md",
    ]
    # sess2's entry names sess's dir (not its own) → untouched.
    row2 = conn.execute("SELECT bootloader_files FROM threads WHERE id='sess2'").fetchone()
    assert json.loads(row2["bootloader_files"]) == ["/ws/working/sess/foreign.md"]
    conn.close()
