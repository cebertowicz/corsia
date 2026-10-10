"""One coalesced notice per changed bootloader path, however it was written.

Task 3's rule: a changed bootloader path becomes one passive context notice on
the thread's next independent activity. Detection lives where a write is
already observed, so there is no per-thread accepted-version ledger and no
re-hash of every source on every turn.
"""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path

import pytest


@pytest.fixture(autouse=True)
def _selected_workspace(tmp_path, monkeypatch):
    """Select the ``_workspace`` root so logical project refs resolve."""
    from infra import config
    from infra.platform_paths import WorkspaceSelection

    monkeypatch.setattr(
        config,
        "WORKSPACE_SELECTION",
        WorkspaceSelection(
            workspace_id="workspace-123",
            root=(tmp_path / "workspace").resolve(),
            name="Primary",
        ),
        raising=False,
    )


def _project_ref(slug: str) -> str:
    return f"workspace:/" f"/workspace-123/projects/{slug}"


def _db() -> sqlite3.Connection:
    from infra.schema import SCHEMA_SQL

    db = sqlite3.connect(":memory:")
    db.row_factory = sqlite3.Row
    db.execute("PRAGMA foreign_keys=ON")
    db.executescript(SCHEMA_SQL)
    db.execute(
        "INSERT INTO projects (id,name,path,created_at,updated_at) "
        "VALUES ('project','Project',?,'t0','t0')",
        (_project_ref("project"),),
    )
    for thread_id in ("author", "reader"):
        db.execute(
            "INSERT INTO threads "
            "(id,user_id,project_id,model,created_at,updated_at) "
            "VALUES (?, 'user', 'project', 'native/model', 't0', 't0')",
            (thread_id,),
        )
    return db


def _touch(path: Path) -> Path:
    """Create the file. The observer only ever sees a *confirmed* write, so a
    path that does not exist is not a case this classifier can receive."""
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("x", encoding="utf-8")
    return path


def _workspace(tmp_path: Path) -> Path:
    root = tmp_path / "workspace"
    (root / "context").mkdir(parents=True)
    (root / "context" / "roles" / "agent").mkdir(parents=True)
    (root / "projects" / "project" / "context").mkdir(parents=True)
    (root / "projects" / "project" / ".threads" / "author" / "context").mkdir(
        parents=True
    )
    return root


def _classify(path: Path, workspace: Path, db: sqlite3.Connection):
    """Return the single address a path resolves to, or None for an ordinary file."""
    from core.context_source_paths import classify_context_source

    changes = classify_context_source(path, workspace=workspace, db=db)
    assert len(changes) <= 1, changes
    return changes[0] if changes else None


def test_workspace_context_file_is_a_workspace_source(tmp_path):
    db = _db()
    workspace = _workspace(tmp_path)
    try:
        change = _classify(_touch(workspace / "context" / "PROJECT.md"), workspace, db)
    finally:
        db.close()

    assert change is not None
    assert (change.object_kind, change.object_id) == ("workspace_context", "workspace")
    assert change.label == "context/PROJECT.md"


def test_role_context_file_is_a_source_at_its_own_scope(tmp_path):
    db = _db()
    workspace = _workspace(tmp_path)
    try:
        change = _classify(
            _touch(
                workspace / "context" / "roles" / "agent"
                / "00-operating-contract.md"
            ),
            workspace,
            db,
        )
    finally:
        db.close()

    assert change is not None
    assert change.object_kind == "workspace_context"
    assert change.label == "context/roles/agent/00-operating-contract.md"


def test_project_context_file_resolves_to_its_project_id(tmp_path):
    db = _db()
    workspace = _workspace(tmp_path)
    try:
        change = _classify(
            workspace / "projects" / "project" / "context" / "PROJECT.md",
            workspace,
            db,
        )
    finally:
        db.close()

    assert change is not None
    assert (change.object_kind, change.object_id) == ("project_context", "project")
    assert change.label == "context/PROJECT.md"


def test_percent_encoded_project_folder_is_attributed_to_its_project(tmp_path):
    db = _db()
    workspace = _workspace(tmp_path)
    planning = workspace / "projects" / "my proj"
    planning.mkdir(parents=True)
    db.execute("UPDATE projects SET path = ? WHERE id = 'project'", (_project_ref("my%20proj"),))
    try:
        change = _classify(_touch(planning / "context" / "PROJECT.md"), workspace, db)
    finally:
        db.close()

    assert change is not None
    assert (change.object_kind, change.object_id) == ("project_context", "project")


def test_thread_context_file_resolves_to_its_thread(tmp_path):
    db = _db()
    workspace = _workspace(tmp_path)
    try:
        change = _classify(
            workspace
            / "projects"
            / "project"
            / ".threads"
            / "author"
            / "context"
            / "RESPONSIBILITY.md",
            workspace,
            db,
        )
    finally:
        db.close()

    assert change is not None
    assert (change.object_kind, change.object_id) == ("thread_context", "author")
    assert change.label == "context/RESPONSIBILITY.md"


def test_ordinary_and_nested_paths_are_not_context_sources(tmp_path):
    db = _db()
    workspace = _workspace(tmp_path)
    try:
        assert _classify(workspace / "source" / "main.py", workspace, db) is None
        assert _classify(workspace / "context" / "notes.txt", workspace, db) is None
        assert (
            _classify(workspace / "context" / "deep" / "nested.md", workspace, db)
            is None
        )
        assert _classify(Path("context/PROJECT.md"), workspace, db) is None
    finally:
        db.close()


def test_edits_to_one_path_collapse_but_two_paths_both_appear(tmp_path, monkeypatch):
    from infra import passive_changes
    from core.context_source_paths import append_context_source_change
    from infra.passive_changes import materialize_pending_digest

    db = _db()
    workspace = _workspace(tmp_path)
    try:
        monkeypatch.setattr(passive_changes, "clock_event", lambda *_args: None)
        for _ in range(3):
            append_context_source_change(
                db,
                path=_touch(workspace / "context" / "PROJECT.md"),
                workspace=workspace,
                actor_thread_id="author",
            )
        append_context_source_change(
            db,
            path=_touch(workspace / "context" / "VISION.md"),
            workspace=workspace,
            actor_thread_id="author",
        )

        accepted = materialize_pending_digest(db, "reader")
        assert accepted is not None
        row = db.execute(
            "SELECT content, metadata FROM messages WHERE id=?",
            (accepted.message_id,),
        ).fetchone()
    finally:
        db.close()

    events = json.loads(row["metadata"])["context_events"]
    labels = sorted(event["details"]["path"] for event in events)
    assert labels == ["context/PROJECT.md", "context/VISION.md"]
    assert row["content"].count("context/PROJECT.md") == 1
    assert "context/VISION.md" in row["content"]


def test_the_writing_thread_is_not_notified_of_its_own_edit(tmp_path, monkeypatch):
    from infra import passive_changes
    from core.context_source_paths import append_context_source_change
    from infra.passive_changes import materialize_pending_digest

    db = _db()
    workspace = _workspace(tmp_path)
    try:
        monkeypatch.setattr(passive_changes, "clock_event", lambda *_args: None)
        append_context_source_change(
            db,
            path=_touch(workspace / "context" / "PROJECT.md"),
            workspace=workspace,
            actor_thread_id="author",
        )
        assert materialize_pending_digest(db, "author") is None
        assert materialize_pending_digest(db, "reader") is not None
    finally:
        db.close()


def test_a_non_context_write_journals_nothing(tmp_path):
    from core.context_source_paths import append_context_source_change

    db = _db()
    workspace = _workspace(tmp_path)
    try:
        assert (
            append_context_source_change(
                db,
                path=workspace / "source" / "main.py",
                workspace=workspace,
                actor_thread_id="author",
            )
            is None
        )
        assert (
            db.execute("SELECT COUNT(*) FROM context_change_journal").fetchone()[0] == 0
        )
    finally:
        db.close()


def test_a_repository_thread_tray_outside_the_workspace_is_not_a_context_source(
    tmp_path,
):
    """A registered repo may contain `.threads/<id>/context/*.md` of its own.

    The observer sees writes in repositories and worktrees too, so scanning for
    `.threads` without proving workspace containment would attribute an
    unrelated repository file to a real CORSIA thread.
    """
    from core.context_source_paths import append_context_source_change

    db = _db()
    workspace = _workspace(tmp_path)
    outside = tmp_path / "some-repo" / ".threads" / "author" / "context" / "x.md"
    try:
        assert (
            append_context_source_change(
                db, path=outside, workspace=workspace, actor_thread_id="author",
            )
            is None
        )
        assert (
            db.execute("SELECT COUNT(*) FROM context_change_journal").fetchone()[0] == 0
        )
    finally:
        db.close()


def test_only_roles_the_loader_reads_produce_a_notice(tmp_path):
    """`ROLE_ORDER` is closed: agent and conductor. Other role dirs load nothing."""
    from core.context_source_paths import append_context_source_change

    db = _db()
    workspace = _workspace(tmp_path)
    roles = workspace / "context" / "roles"
    try:
        assert (
            append_context_source_change(
                db, path=roles / "agent" / "00-contract.md",
                workspace=workspace, actor_thread_id="author",
            )
            is not None
        )
        assert (
            append_context_source_change(
                db, path=roles / "reviewer" / "00-contract.md",
                workspace=workspace, actor_thread_id="author",
            )
            is None
        )
        rows = db.execute(
            "SELECT details_json FROM context_change_journal"
        ).fetchall()
        assert len(rows) == 1
        assert json.loads(rows[0]["details_json"])["path"] == (
            "context/roles/agent/00-contract.md"
        )
    finally:
        db.close()


def test_an_ordinary_project_write_costs_no_project_lookup(tmp_path):
    """The lexical shape is tested before any database work.

    Every observed write under `projects/` used to load the whole projects
    table; only a path already shaped like a context file should pay for that.
    """
    from core.context_source_paths import append_context_source_change

    db = _db()
    workspace = _workspace(tmp_path)
    queries: list[str] = []

    class _Counting:
        """Proxy that records SQL; sqlite3.Connection.execute is read-only."""

        def __init__(self, inner):
            self._inner = inner

        def execute(self, sql, *args, **kwargs):
            queries.append(sql)
            return self._inner.execute(sql, *args, **kwargs)

        def __getattr__(self, name):
            return getattr(self._inner, name)

    try:
        assert (
            append_context_source_change(
                _Counting(db),
                path=workspace / "projects" / "corsia" / "source" / "main.py",
                workspace=workspace,
                actor_thread_id="author",
            )
            is None
        )
        assert not any("FROM projects" in sql for sql in queries)
    finally:
        db.close()


def test_a_configured_file_outside_context_still_notifies(tmp_path):
    """The configured list is the authority, not the directory shape.

    A scope's effective sources are its configured list *plus* a `context/`
    scan, and the bootloader API allows configured entries outside `context/`.
    Those are live prefix material, so a shape-only rule would silently miss
    exactly the files Task 3 exists to notice.
    """
    from core.context_source_paths import append_context_source_change

    db = _db()
    workspace = _workspace(tmp_path)
    guide = workspace / "notes" / "house-style.md"
    guide.parent.mkdir(parents=True, exist_ok=True)
    guide.write_text("x", encoding="utf-8")
    db.execute(
        "UPDATE threads SET bootloader_files=? WHERE id='author'",
        (json.dumps([str(guide)]),),
    )
    try:
        assert (
            append_context_source_change(
                db, path=guide, workspace=workspace, actor_thread_id="reader",
            )
            is not None
        )
        rows = db.execute(
            "SELECT object_kind,object_id FROM context_change_journal"
        ).fetchall()
        assert len(rows) == 1
        assert rows[0]["object_kind"] == "thread_context"
        assert rows[0]["object_id"] == "author"
    finally:
        db.close()


def test_a_tray_file_the_thread_does_not_load_is_not_attributed_to_it(tmp_path):
    """Sitting in `.threads/<id>/` is not proof the thread loads the file."""
    from core.context_source_paths import append_context_source_change

    db = _db()
    workspace = _workspace(tmp_path)
    stray = workspace / ".threads" / "author" / "scratch" / "notes.md"
    try:
        assert (
            append_context_source_change(
                db, path=stray, workspace=workspace, actor_thread_id="author",
            )
            is None
        )
        assert (
            db.execute("SELECT COUNT(*) FROM context_change_journal").fetchone()[0] == 0
        )
    finally:
        db.close()


def test_project_resolution_confirms_one_candidate_not_every_project(tmp_path):
    """The planning root names its project, so only that one is confirmed."""
    from core.context_source_paths import append_context_source_change

    db = _db()
    workspace = _workspace(tmp_path)
    for extra in ("alpha", "beta", "gamma"):
        db.execute(
            "INSERT INTO projects (id,name,path,created_at,updated_at) VALUES (?,?,?,?,?)",
            (extra, extra.title(), _project_ref(extra), "t0", "t0"),
        )
    resolves: list[str] = []
    import core.project_context as project_context

    real_resolve = project_context.resolve_project_paths

    def _counting(workspace_arg, project_id, **kwargs):
        resolves.append(project_id)
        return real_resolve(workspace_arg, project_id, **kwargs)

    project_context.resolve_project_paths = _counting
    try:
        append_context_source_change(
            db,
            path=_touch(
                workspace / "projects" / "project" / "context" / "PROJECT.md"
            ),
            workspace=workspace,
            actor_thread_id="author",
        )
        assert len(resolves) <= 1, f"resolved {resolves} once per project row"
    finally:
        project_context.resolve_project_paths = real_resolve
        db.close()


def test_every_thread_that_configures_a_file_is_notified(tmp_path):
    """Two threads loading one configured file both receive a journal row.

    Attribution is per loader, not first-match: the second thread must not be
    left with stale instructions because another thread was found first.
    """
    from core.context_source_paths import append_context_source_change

    db = _db()
    workspace = _workspace(tmp_path)
    guide = _touch(workspace / "notes" / "house-style.md")
    db.execute(
        "UPDATE threads SET bootloader_files=? WHERE id IN ('author','reader')",
        (json.dumps([str(guide)]),),
    )
    try:
        append_context_source_change(
            db, path=guide, workspace=workspace, actor_thread_id="author",
        )
        rows = db.execute(
            "SELECT object_kind,object_id FROM context_change_journal ORDER BY object_id"
        ).fetchall()
    finally:
        db.close()

    assert [(r["object_kind"], r["object_id"]) for r in rows] == [
        ("thread_context", "author"),
        ("thread_context", "reader"),
    ]


def test_a_non_ascii_configured_filename_is_noticed(tmp_path):
    """`bootloader_files` is stored json.dumps-escaped, so `café.md` is
    `caf\\u00e9.md` on disk; the candidate lookup must match the stored form."""
    from core.context_source_paths import append_context_source_change

    db = _db()
    workspace = _workspace(tmp_path)
    guide = _touch(workspace / "notes" / "café.md")
    db.execute(
        "UPDATE threads SET bootloader_files=? WHERE id='author'",
        (json.dumps([str(guide)]),),
    )
    try:
        assert (
            append_context_source_change(
                db, path=guide, workspace=workspace, actor_thread_id="reader",
            )
            is not None
        )
        rows = db.execute(
            "SELECT object_kind,object_id FROM context_change_journal"
        ).fetchall()
    finally:
        db.close()

    assert [(r["object_kind"], r["object_id"]) for r in rows] == [
        ("thread_context", "author"),
    ]


def test_a_wider_scope_notice_is_not_suppressed_by_a_thread_configuration(tmp_path):
    """A project context file configured by threads still notifies the project.

    A thread inside the project already receives the project row, so it gets
    no second row; a thread outside the project is reached only by its own row.
    """
    from core.context_source_paths import append_context_source_change

    db = _db()
    workspace = _workspace(tmp_path)
    project_file = _touch(workspace / "projects" / "project" / "context" / "PROJECT.md")
    db.execute(
        "INSERT INTO threads (id,user_id,project_id,model,created_at,updated_at) "
        "VALUES ('outsider','user',NULL,'native/model','t0','t0')"
    )
    db.execute(
        "UPDATE threads SET bootloader_files=? WHERE id IN ('author','outsider')",
        (json.dumps([str(project_file)]),),
    )
    try:
        append_context_source_change(
            db, path=project_file, workspace=workspace, actor_thread_id="reader",
        )
        rows = db.execute(
            "SELECT object_kind,object_id FROM context_change_journal "
            "ORDER BY object_kind, object_id"
        ).fetchall()
    finally:
        db.close()

    assert [(r["object_kind"], r["object_id"]) for r in rows] == [
        ("project_context", "project"),
        ("thread_context", "outsider"),
    ]


def test_a_repo_tray_inside_the_workspace_is_not_the_thread_s_tray(tmp_path):
    """Containment under the workspace is not enough to prove a tray.

    A registered repository lives at `projects/<slug>/source/<name>/`, which is
    itself inside the workspace, so a `.threads/<real-id>/context/*.md` there
    passes any containment check while being a file the thread never loads.
    The tray must come from the thread's own scope.
    """
    from core.context_source_paths import append_context_source_change

    db = _db()
    workspace = _workspace(tmp_path)
    repo_tray = (
        workspace / "projects" / "project" / "source" / "corsia"
        / ".threads" / "author" / "context" / "RESPONSIBILITY.md"
    )
    try:
        assert (
            append_context_source_change(
                db, path=_touch(repo_tray), workspace=workspace,
                actor_thread_id="reader",
            )
            is None
        )
        assert (
            db.execute("SELECT COUNT(*) FROM context_change_journal").fetchone()[0] == 0
        )
    finally:
        db.close()
