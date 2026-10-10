
from __future__ import annotations

import json
import os
import sqlite3
import sys
import uuid
from pathlib import Path

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))

from api.project_scaffolding import validate_bootloader_files  # noqa: E402
from auth_fixtures import OWNER_TOKEN

_TOKEN = OWNER_TOKEN


def _unique_name(prefix: str) -> str:
    return f"{prefix}-{uuid.uuid4().hex[:8]}"


_WORKSPACE_ID = "workspace-123"


@pytest.fixture(autouse=True)
def _selected_workspace(request, tmp_path, monkeypatch):
    """Select ``tmp_path`` as the workspace so logical project refs resolve.

    Integration tests run against their own selected workspace.
    """
    if "integration_client" in request.fixturenames:
        return
    from infra import config
    from infra.platform_paths import WorkspaceSelection

    monkeypatch.setattr(
        config,
        "WORKSPACE_SELECTION",
        WorkspaceSelection(
            workspace_id=_WORKSPACE_ID, root=tmp_path.resolve(), name="Primary"
        ),
        raising=False,
    )


def _ref(project_dir: Path) -> str:
    """Return the logical workspace reference stored for ``projects/<slug>``."""
    return f"workspace:/" f"/{_WORKSPACE_ID}/projects/{project_dir.name}"



def test_validator_rules(tmp_path):
    (tmp_path / "BRIEF.md").write_text("x")
    (tmp_path / "specs").mkdir()
    (tmp_path / "specs" / "a.md").write_text("x")

    assert validate_bootloader_files(tmp_path, ["BRIEF.md", "specs/a.md"]) is None
    assert validate_bootloader_files(tmp_path, []) is None, "empty = inject nothing, valid"
    assert "relative" in validate_bootloader_files(tmp_path, ["/etc/passwd"])
    assert "relative" in validate_bootloader_files(tmp_path, ["../escape.md"])
    assert "can be bootloaded" in validate_bootloader_files(tmp_path, ["app.py"])
    assert "not found" in validate_bootloader_files(tmp_path, ["nope.md"])
    assert "twice" in validate_bootloader_files(tmp_path, ["BRIEF.md", "BRIEF.md"])


def test_new_project_scaffold_seeds_canonical_context_directory(tmp_path, monkeypatch):
    from api import project_scaffolding

    monkeypatch.setattr(project_scaffolding.config, "WORKSPACE", tmp_path)
    project = project_scaffolding._scaffold_project_workspace("Demo", "A demo")

    assert (project / "context" / "PROJECT.md").is_file()
    assert not (project / "context" / "BRIEF.md").exists()
    assert (project / "shared" / "INDEX.md").is_file()
    assert (project / "skills").is_dir()
    assert (project / ".threads").is_dir()
    assert (project / "source").is_dir()
    assert not (project / "PROJECT.md").exists()
    assert not (project / "BRIEF.md").exists()
    assert not (project / "working").exists()


def test_model_injection_ignores_legacy_designation_and_uses_context_dir(
    tmp_path, monkeypatch
):
    from core import project_context

    ws = tmp_path / "projects" / "demo"
    (ws / "specs").mkdir(parents=True)
    (ws / "context").mkdir()
    (ws / "PROJECT.md").write_text("project instructions")
    (ws / "BRIEF.md").write_text("the brief")
    (ws / "specs" / "extra.md").write_text("extra spec")
    (ws / "context" / "PROJECT.md").write_text("canonical project context")
    (ws / "context" / "LIVE.md").write_text("live context")
    (ws / "context" / "COORDINATION-MODEL.md").write_text("coordination")

    conn = sqlite3.connect(":memory:", check_same_thread=False)
    conn.row_factory = sqlite3.Row
    conn.execute(
        "CREATE TABLE projects (id TEXT PRIMARY KEY, name TEXT, path TEXT, bootloader_files TEXT)"
    )
    conn.execute(
        "INSERT INTO projects VALUES ('p1', 'Demo', ?, ?)",
        (_ref(ws), json.dumps(["BRIEF.md", "specs/extra.md", "/etc/passwd", "../x.md"])),
    )
    conn.execute("INSERT INTO projects VALUES ('p2', 'Demo', ?, NULL)", (_ref(ws),))
    conn.commit()

    class _NoClose:
        def __init__(self, real): self._real = real
        def __getattr__(self, name): return getattr(self._real, name)
        def close(self): pass

    import infra.database
    monkeypatch.setattr(infra.database, "get_db", lambda: _NoClose(conn))

    out = project_context.load_project_context(tmp_path, "p1")
    assert "live context" in out
    assert "the brief" not in out and "extra spec" not in out
    assert "project instructions" not in out
    assert "/etc/passwd" not in out and "../x.md" not in out

    sources = project_context.project_context_source_entries(tmp_path, "p1")
    by_rel_path = {source.metadata["relative_path"]: source for source in sources}
    assert set(by_rel_path) == {
        "context/PROJECT.md",
        "context/LIVE.md",
        "context/COORDINATION-MODEL.md",
    }
    assert by_rel_path["context/LIVE.md"].owner_surface == "document-viewer"
    assert by_rel_path["context/LIVE.md"].stable_reference == str(
        (ws / "context" / "LIVE.md").resolve()
    )
    assert "live context" in (by_rel_path["context/LIVE.md"].prompt_content or "")

    out2 = project_context.load_project_context(tmp_path, "p2")
    assert "live context" in out2
    assert "project instructions" not in out2 and "the brief" not in out2


def test_model_injection_never_merges_registered_repo_context(tmp_path, monkeypatch):
    """Only the canonical project workspace owns project model context."""
    from core import project_context

    project_workspace = tmp_path / "projects" / "demo"
    (project_workspace / "context").mkdir(parents=True)
    (project_workspace / "context" / "PROJECT.md").write_text("CANONICAL_CONTEXT")
    (project_workspace / "context" / "COORDINATION-MODEL.md").write_text(
        "CANONICAL_COORDINATION"
    )

    registered_repo = tmp_path / "registered-repo"
    (registered_repo / "context").mkdir(parents=True)
    (registered_repo / "context" / "REPO.md").write_text("REPO_CONTEXT_MUST_NOT_LOAD")

    conn = sqlite3.connect(":memory:", check_same_thread=False)
    conn.row_factory = sqlite3.Row
    conn.execute(
        "CREATE TABLE projects (id TEXT PRIMARY KEY, name TEXT, path TEXT, bootloader_files TEXT)"
    )
    conn.execute(
        "INSERT INTO projects VALUES ('p1', 'Demo', ?, ?)",
        (_ref(project_workspace), json.dumps(["PROJECT.md"])),
    )
    conn.commit()

    class _NoClose:
        def __init__(self, real): self._real = real
        def __getattr__(self, name): return getattr(self._real, name)
        def close(self): pass

    import infra.database
    monkeypatch.setattr(infra.database, "get_db", lambda: _NoClose(conn))

    out = project_context.load_project_context(tmp_path, "p1")
    assert "CANONICAL_CONTEXT" in out
    assert "REPO_CONTEXT_MUST_NOT_LOAD" not in out
    sources = project_context.project_context_source_entries(tmp_path, "p1", db=conn)
    assert [source.metadata["relative_path"] for source in sources] == [
        "context/PROJECT.md",
        "context/COORDINATION-MODEL.md",
    ]


def test_load_project_context_renders_registered_repository_list(
    tmp_path,
):
    """A multi-repo project renders the registered list."""
    from core import project_context

    ws = tmp_path / "projects" / "demo"
    (ws / "context").mkdir(parents=True)
    (ws / "context" / "PROJECT.md").write_text("canonical project context")
    (ws / "context" / "COORDINATION-MODEL.md").write_text("coordination")

    conn = sqlite3.connect(":memory:", check_same_thread=False)
    conn.row_factory = sqlite3.Row
    conn.execute(
        "CREATE TABLE projects (id TEXT PRIMARY KEY, name TEXT, path TEXT, bootloader_files TEXT)"
    )
    conn.execute(
        "INSERT INTO projects VALUES ('p1', 'Demo', ?, NULL)",
        (_ref(ws),),
    )
    conn.execute(
        "CREATE TABLE project_repo_policies ("
        " project_id TEXT NOT NULL, repo_name TEXT NOT NULL, repo_path TEXT NOT NULL,"
        " pinned_branch TEXT, protect_base_checkout INTEGER NOT NULL,"
        " PRIMARY KEY (project_id, repo_name))"
    )
    conn.execute(
        "INSERT INTO project_repo_policies VALUES ('p1', 'app', ?, 'dev', 1)",
        (str(tmp_path / "repos" / "app"),),
    )
    conn.execute(
        "INSERT INTO project_repo_policies VALUES ('p1', 'tools', ?, NULL, 0)",
        (str(tmp_path / "repos" / "tools"),),
    )
    conn.commit()

    out = project_context.load_project_context(tmp_path, "p1", db=conn)

    assert "Registered repositories:" in out
    assert "- app" in out
    assert "pinned=dev" in out
    assert "protected" in out
    assert "- tools" in out
    assert "Project repo:" not in out
    assert "canonical project context" in out


def test_load_project_context_header_has_no_project_repo_line(tmp_path):
    """Without registered policies the header renders no repository line."""
    from core import project_context

    ws = tmp_path / "projects" / "demo"
    (ws / "context").mkdir(parents=True)
    (ws / "context" / "PROJECT.md").write_text("canonical project context")
    (ws / "context" / "COORDINATION-MODEL.md").write_text("coordination")

    conn = sqlite3.connect(":memory:", check_same_thread=False)
    conn.row_factory = sqlite3.Row
    conn.execute(
        "CREATE TABLE projects (id TEXT PRIMARY KEY, name TEXT, path TEXT, bootloader_files TEXT)"
    )
    conn.execute(
        "INSERT INTO projects VALUES ('p1', 'Demo', ?, NULL)",
        (_ref(ws),),
    )
    conn.commit()

    out = project_context.load_project_context(tmp_path, "p1", db=conn)

    assert out.startswith(f"# Project: Demo\nProject workspace: {ws.resolve()}\n")
    assert "Registered repositories:" not in out
    assert "Project repo:" not in out


def test_load_project_context_surfaces_policy_lookup_failure(
    tmp_path,
    monkeypatch,
):
    """A failed registered-repo lookup surfaces; it never renders other bytes.

    These lines live in the cache-stable prefix, so a transient failure must
    not silently swap in a different string — that would both disguise a
    multi-repo project as single-repo and invalidate the prefix cache.
    """
    from core import project_context

    ws = tmp_path / "projects" / "demo"
    (ws / "context").mkdir(parents=True)
    (ws / "context" / "PROJECT.md").write_text("canonical project context")
    (ws / "context" / "COORDINATION-MODEL.md").write_text("coordination")

    conn = sqlite3.connect(":memory:", check_same_thread=False)
    conn.row_factory = sqlite3.Row
    conn.execute(
        "CREATE TABLE projects (id TEXT PRIMARY KEY, name TEXT, path TEXT, bootloader_files TEXT)"
    )
    conn.execute(
        "INSERT INTO projects VALUES ('p1', 'Demo', ?, NULL)",
        (_ref(ws),),
    )
    conn.commit()

    import infra.project_repo_policy

    def _boom(*_args, **_kwargs):
        raise RuntimeError("policy db unavailable")

    monkeypatch.setattr(
        infra.project_repo_policy,
        "get_project_repo_policies",
        _boom,
    )

    with pytest.raises(RuntimeError, match="registered repository policy"):
        project_context.load_project_context(tmp_path, "p1", db=conn)


def test_project_rename_keeps_persisted_canonical_context(tmp_path):
    """A display-name change must not retarget model context to another slug."""
    from core import project_context

    old_workspace = tmp_path / "projects" / "old-name"
    (old_workspace / "context").mkdir(parents=True)
    (old_workspace / "context" / "PROJECT.md").write_text("EXPECTED_CONTEXT")
    (old_workspace / "context" / "COORDINATION-MODEL.md").write_text(
        "EXPECTED_COORDINATION"
    )
    derived_from_new_name = tmp_path / "projects" / "renamed-project"
    (derived_from_new_name / "context").mkdir(parents=True)
    (derived_from_new_name / "context" / "PROJECT.md").write_text("WRONG_CONTEXT")
    (derived_from_new_name / "context" / "COORDINATION-MODEL.md").write_text(
        "WRONG_COORDINATION"
    )

    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    conn.execute("CREATE TABLE projects (id TEXT PRIMARY KEY, name TEXT, path TEXT)")
    conn.execute(
        "INSERT INTO projects VALUES ('p1', 'Renamed Project', ?)",
        (_ref(old_workspace),),
    )

    name, workspace_dir = project_context.resolve_project_paths(
        tmp_path,
        "p1",
        db=conn,
    )
    assert name == "Renamed Project"
    assert workspace_dir == old_workspace.resolve()
    sources = project_context.project_context_source_entries(tmp_path, "p1", db=conn)
    rendered = "\n".join(source.prompt_content or "" for source in sources)
    assert "EXPECTED_CONTEXT" in rendered
    assert "WRONG_CONTEXT" not in rendered


def test_project_context_lookup_propagates_database_failure(tmp_path):
    """A broken source-of-truth lookup must stop dispatch, not omit context."""
    from core import project_context

    class BrokenDb:
        def execute(self, *_args, **_kwargs):
            raise sqlite3.OperationalError("database unavailable")

    with pytest.raises(sqlite3.OperationalError, match="database unavailable"):
        project_context.resolve_project_paths(tmp_path, "p1", db=BrokenDb())


def test_missing_persisted_workspace_never_falls_through_to_renamed_slug(tmp_path):
    """An unavailable canonical path must not cross-wire another project."""
    from core import project_context

    missing_workspace = tmp_path / "projects" / "old-name"
    derived_from_new_name = tmp_path / "projects" / "renamed-project"
    (derived_from_new_name / "context").mkdir(parents=True)
    (derived_from_new_name / "context" / "PROJECT.md").write_text(
        "WRONG_PROJECT_CONTEXT"
    )

    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    conn.execute("CREATE TABLE projects (id TEXT PRIMARY KEY, name TEXT, path TEXT)")
    conn.execute(
        "INSERT INTO projects VALUES ('p1', 'Renamed Project', ?)",
        (_ref(missing_workspace),),
    )

    _name, workspace_dir = project_context.resolve_project_paths(
        tmp_path,
        "p1",
        db=conn,
    )
    assert workspace_dir == missing_workspace.resolve(strict=False)
    with pytest.raises(RuntimeError, match="missing required project context"):
        project_context.project_context_source_entries(tmp_path, "p1", db=conn)


def test_system_context_dir_auto_loads_md_files(tmp_path):
    from core.workspace_context_sources import workspace_context_files

    (tmp_path / "context").mkdir()
    (tmp_path / "context" / "foo.md").write_text("# foo\ndir-as-truth")

    files = workspace_context_files(tmp_path)
    by_name = {f.name: f for f in files}

    assert "context/foo.md" in by_name
    row = by_name["context/foo.md"]
    assert row.exists is True
    assert row.path == (tmp_path / "context" / "foo.md").resolve()
    assert row.path.read_text() == "# foo\ndir-as-truth"


def test_system_context_empty_config_yields_dir_scan_only(tmp_path):
    # Directory-as-truth (Phase 1b/1c): no hardcoded static list. With no
    # configured files and no context/ dir, the static set is empty.
    from core.workspace_context_sources import workspace_context_bootloader_files, workspace_context_files

    assert workspace_context_bootloader_files(workspace=tmp_path) == []

    (tmp_path / "context").mkdir()
    assert workspace_context_bootloader_files(workspace=tmp_path) == []

    (tmp_path / "context" / "CORSIA.md").write_text("# CORSIA")
    (tmp_path / "context" / "IDENTITY.md").write_text("# Identity")
    files = workspace_context_bootloader_files(workspace=tmp_path)
    assert files == ["context/CORSIA.md", "context/IDENTITY.md"], "dir-scan sorted, sole truth"

    rows = [row.name for row in workspace_context_files(tmp_path)]
    assert rows == files


def test_system_context_dir_dedupes_against_configured_list(tmp_path):
    # A transiently-configured entry (pre-cutover) wins over a same-basename
    # dir-scanned file; other dir files still append after it.
    import infra.database
    from core.workspace_context_sources import workspace_context_bootloader_files

    conn = sqlite3.connect(":memory:", check_same_thread=False)
    conn.row_factory = sqlite3.Row
    conn.execute("CREATE TABLE settings (key TEXT PRIMARY KEY, value TEXT, updated_at TEXT)")
    conn.execute(
        "INSERT INTO settings VALUES ('workspace_context', ?, 'now')",
        (json.dumps({"bootloader_files": ["AGENTS.md"]}),),
    )
    conn.commit()

    (tmp_path / "context").mkdir()
    (tmp_path / "context" / "AGENTS.md").write_text("duplicate of configured AGENTS.md")
    (tmp_path / "context" / "new-one.md").write_text("only in context dir")

    files = workspace_context_bootloader_files(db=conn, workspace=tmp_path)
    assert files.count("AGENTS.md") == 1
    assert "context/AGENTS.md" not in files, "configured entry wins; no duplicate for same basename"
    assert "context/new-one.md" in files
    assert files.index("AGENTS.md") < files.index("context/new-one.md"), "configured files come first"


def test_relocation_is_content_identical_system_scope(tmp_path):
    # Proof that Phase 1b is a pure relocation for the system scope: the set of
    # (basename -> injected content) is identical whether files are configured at
    # the workspace root (old) or dir-scanned from context/ with empty config (new).
    from core.workspace_context_sources import workspace_context_source_entries

    payloads = {
        "CORSIA.md": "# CORSIA\nplatform truth",
        "CORE-CONSTRAINTS.md": "# Constraints\nthe 9 rules",
        "IDENTITY.md": "# Identity\nJarvis",
    }

    # --- OLD world: files at workspace root, configured by absolute path ---
    old_ws = tmp_path / "old"
    old_ws.mkdir()
    for name, body in payloads.items():
        (old_ws / name).write_text(body)
    old_conn = sqlite3.connect(":memory:")
    old_conn.row_factory = sqlite3.Row
    old_conn.execute("CREATE TABLE settings (key TEXT PRIMARY KEY, value TEXT, updated_at TEXT)")
    old_conn.execute(
        "INSERT INTO settings VALUES ('workspace_context', ?, 'now')",
        (json.dumps({"bootloader_files": [str(old_ws / n) for n in payloads]}),),
    )
    old_conn.commit()
    old_entries = workspace_context_source_entries(old_ws, db=old_conn)

    # --- NEW world: files in context/, empty configured list ---
    new_ws = tmp_path / "new"
    (new_ws / "context").mkdir(parents=True)
    for name, body in payloads.items():
        (new_ws / "context" / name).write_text(body)
    new_conn = sqlite3.connect(":memory:")
    new_conn.row_factory = sqlite3.Row
    new_conn.execute("CREATE TABLE settings (key TEXT PRIMARY KEY, value TEXT, updated_at TEXT)")
    new_conn.execute(
        "INSERT INTO settings VALUES ('workspace_context', ?, 'now')",
        (json.dumps({"bootloader_files": []}),),
    )
    new_conn.commit()
    new_entries = workspace_context_source_entries(new_ws, db=new_conn)

    def _body_by_basename(entries):
        # Strip the "## <name>\n\n" section header — it encodes the source path,
        # which necessarily changes on relocation (abs path -> context/<name>).
        out = {}
        for e in entries:
            base = Path(e.metadata["name"]).name
            body = e.prompt_content.split("\n\n", 1)[1] if e.prompt_content else None
            out[base] = body
        return out

    old_bodies = _body_by_basename(old_entries)
    new_bodies = _body_by_basename(new_entries)
    assert set(old_bodies) == set(payloads), "old world injects exactly the configured files"
    assert set(new_bodies) == set(old_bodies), "same set of files — pure relocation"
    assert new_bodies == old_bodies, "same injected file content, byte-for-byte"

    # The one intended structural delta: the section-header label moves from the
    # absolute source path to the relative context/ path. Documented, not hidden.
    old_headers = {Path(e.metadata["name"]).name: e.metadata["name"] for e in old_entries}
    new_headers = {Path(e.metadata["name"]).name: e.metadata["name"] for e in new_entries}
    assert old_headers["CORSIA.md"].endswith("/old/CORSIA.md")
    assert new_headers["CORSIA.md"] == "context/CORSIA.md"


class _NoCloseConn:
    def __init__(self, real): self._real = real
    def __getattr__(self, name): return getattr(self._real, name)
    def close(self): pass


def _register_project(monkeypatch, project_id: str, name: str, ws: Path) -> None:
    conn = sqlite3.connect(":memory:", check_same_thread=False)
    conn.row_factory = sqlite3.Row
    conn.execute(
        "CREATE TABLE projects (id TEXT PRIMARY KEY, name TEXT, path TEXT, bootloader_files TEXT)"
    )
    conn.execute("INSERT INTO projects VALUES (?, ?, ?, NULL)", (project_id, name, _ref(ws)))
    conn.commit()

    import infra.database
    monkeypatch.setattr(infra.database, "get_db", lambda: _NoCloseConn(conn))


def test_project_context_dir_auto_loads_md_files(tmp_path, monkeypatch):
    from core import project_context

    ws = tmp_path / "projects" / "demo2"
    (ws / "context").mkdir(parents=True)
    (ws / "PROJECT.md").write_text("project instructions")
    (ws / "context" / "PROJECT.md").write_text("canonical project")
    (ws / "context" / "extra.md").write_text("dir-as-truth extra")
    (ws / "context" / "COORDINATION-MODEL.md").write_text("coordination")
    _register_project(monkeypatch, "p1", "Demo2", ws)

    out = project_context.load_project_context(tmp_path, "p1")
    assert "project instructions" not in out
    assert "dir-as-truth extra" in out

    sources = project_context.project_context_source_entries(tmp_path, "p1")
    by_rel_path = {source.metadata["relative_path"]: source for source in sources}
    assert set(by_rel_path) == {
        "context/PROJECT.md",
        "context/extra.md",
        "context/COORDINATION-MODEL.md",
    }
    assert by_rel_path["context/extra.md"].stable_reference == str(
        (ws / "context" / "extra.md").resolve()
    )


def test_project_context_uses_conceptual_authority_order(tmp_path, monkeypatch):
    from core import project_context

    ws = tmp_path / "projects" / "ordered"
    context = ws / "context"
    context.mkdir(parents=True)
    for filename in (
        "COORDINATION-MODEL.md",
        "PROJECT.md",
        "REFERENCE.md",
        "SAFETY-INVARIANTS.md",
        "VISION-CORE.md",
    ):
        (context / filename).write_text(filename, encoding="utf-8")
    _register_project(monkeypatch, "p1", "Ordered", ws)

    sources = project_context.project_context_source_entries(tmp_path, "p1")

    assert [source.metadata["relative_path"] for source in sources] == [
        "context/PROJECT.md",
        "context/VISION-CORE.md",
        "context/SAFETY-INVARIANTS.md",
        "context/REFERENCE.md",
        "context/COORDINATION-MODEL.md",
    ]


def test_project_context_rejects_invalid_utf8_in_required_source(
    tmp_path,
    monkeypatch,
):
    from core import project_context

    ws = tmp_path / "projects" / "invalid-context"
    context = ws / "context"
    context.mkdir(parents=True)
    (context / "PROJECT.md").write_bytes(b"\xff")
    (context / "COORDINATION-MODEL.md").write_text("coordination", encoding="utf-8")
    _register_project(monkeypatch, "p1", "Invalid Context", ws)

    with pytest.raises(RuntimeError, match="required project context.*PROJECT.md"):
        project_context.project_context_source_entries(tmp_path, "p1")


def test_project_context_rejects_unreadable_required_source(tmp_path, monkeypatch):
    from core import project_context

    ws = tmp_path / "projects" / "unreadable-context"
    context = ws / "context"
    context.mkdir(parents=True)
    project_file = context / "PROJECT.md"
    project_file.write_text("project", encoding="utf-8")
    (context / "COORDINATION-MODEL.md").write_text("coordination", encoding="utf-8")
    _register_project(monkeypatch, "p1", "Unreadable Context", ws)
    read_text = Path.read_text

    def fail_project_read(path, *args, **kwargs):
        if path == project_file:
            raise OSError("simulated read failure")
        return read_text(path, *args, **kwargs)

    monkeypatch.setattr(Path, "read_text", fail_project_read)

    with pytest.raises(RuntimeError, match="required project context.*PROJECT.md"):
        project_context.project_context_source_entries(tmp_path, "p1")


@pytest.mark.parametrize(
    ("missing", "present"),
    [
        ("PROJECT.md", "COORDINATION-MODEL.md"),
        ("COORDINATION-MODEL.md", "PROJECT.md"),
    ],
)
def test_project_context_rejects_missing_required_source(
    tmp_path,
    monkeypatch,
    missing,
    present,
):
    from core import project_context

    ws = tmp_path / "projects" / "demo3"
    context = ws / "context"
    context.mkdir(parents=True)
    (context / present).write_text("present", encoding="utf-8")
    _register_project(monkeypatch, "p1", "Demo3", ws)

    with pytest.raises(RuntimeError, match=rf"missing required project context.*{missing}"):
        project_context.load_project_context(tmp_path, "p1")


def test_project_context_rejects_required_source_disappearing_after_discovery(
    tmp_path,
    monkeypatch,
):
    from core import project_context

    ws = tmp_path / "projects" / "racing-context"
    context = ws / "context"
    context.mkdir(parents=True)
    project_file = context / "PROJECT.md"
    coordination_file = context / "COORDINATION-MODEL.md"
    project_file.write_text("project", encoding="utf-8")
    coordination_file.write_text("coordination", encoding="utf-8")
    _register_project(monkeypatch, "p1", "Racing Context", ws)
    read_text = Path.read_text

    def remove_coordination_after_project_read(path, *args, **kwargs):
        content = read_text(path, *args, **kwargs)
        if path == project_file:
            coordination_file.unlink()
        return content

    monkeypatch.setattr(Path, "read_text", remove_coordination_after_project_read)

    with pytest.raises(
        RuntimeError,
        match="required project context.*COORDINATION-MODEL.md",
    ):
        project_context.project_context_source_entries(tmp_path, "p1")


def test_project_context_dir_supersedes_same_named_root_file(tmp_path, monkeypatch):
    from core import project_context

    ws = tmp_path / "projects" / "demo4"
    (ws / "context").mkdir(parents=True)
    (ws / "PROJECT.md").write_text("project instructions")
    (ws / "context" / "PROJECT.md").write_text("duplicate — must not load")
    (ws / "context" / "COORDINATION-MODEL.md").write_text("coordination")
    _register_project(monkeypatch, "p1", "Demo4", ws)

    sources = project_context.project_context_source_entries(tmp_path, "p1")
    by_rel_path = {source.metadata["relative_path"]: source for source in sources}
    assert set(by_rel_path) == {
        "context/PROJECT.md",
        "context/COORDINATION-MODEL.md",
    }
    assert by_rel_path["context/PROJECT.md"].stable_reference == str(
        (ws / "context" / "PROJECT.md").resolve()
    )


@pytest.mark.integration
class TestBootloaderPatch:
    def _mk(self, client) -> tuple[str, str]:
        r = client.post(f"/api/projects?token={_TOKEN}", json={"name": _unique_name("boot")})
        assert r.status_code == 200, r.text
        p = r.json()["project"]
        return p["id"], p["path"]

    def test_project_detail_exposes_directory_backed_context_files(self, integration_client):
        pid, path = self._mk(integration_client)

        detail = integration_client.get(f"/api/projects/{pid}?token={_TOKEN}")

        assert detail.status_code == 200, detail.text
        assert detail.json()["context_files"] == [
            "context/PROJECT.md",
            "context/COORDINATION-MODEL.md",
        ]
        assert json.loads(detail.json()["bootloader_files"]) == []
        assert (Path(path) / "context" / "PROJECT.md").is_file()
        assert not (Path(path) / "context" / "BRIEF.md").exists()

    def test_project_selection_patch_is_retired(self, integration_client):
        pid, _ = self._mk(integration_client)
        for obsolete_selection in (["context/PROJECT.md"], [], None):
            r = integration_client.patch(
                f"/api/projects/{pid}?token={_TOKEN}",
                json={"bootloader_files": obsolete_selection},
            )
            assert r.status_code == 400, r.text
            assert "directory-backed" in r.json()["detail"]

        detail = integration_client.get(f"/api/projects/{pid}?token={_TOKEN}").json()
        assert json.loads(detail["bootloader_files"]) == []
