"""Regression coverage for the agent-readable System workspace projection."""

from __future__ import annotations

import hashlib
import json
import os
import shutil
import sqlite3
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import pytest

from core import system_projection
from core.skills_registry import resolve_effective_skills
from core.system_projection import _agent_readable as real_agent_readable
from core.system_projection import ensure_system_projection
from infra.system_package import coordination_models_dir, system_package_dir


REAL_PACKAGE = system_package_dir()


def _db() -> sqlite3.Connection:
    from infra.schema import SCHEMA_SQL

    db = sqlite3.connect(":memory:")
    db.row_factory = sqlite3.Row
    db.executescript(SCHEMA_SQL)
    db.execute(
        "INSERT INTO threads (id,user_id,project_id,model,created_at,updated_at) "
        "VALUES ('t1','user',NULL,'native/model','t0','t0')"
    )
    return db


@pytest.fixture(autouse=True)
def _clear_projection_cache():
    from core import system_projection

    system_projection._CACHE.clear()
    yield
    system_projection._CACHE.clear()


@pytest.fixture
def copied_package(monkeypatch, tmp_path: Path) -> Path:
    package = tmp_path / "installed-package"
    shutil.copytree(REAL_PACKAGE, package)
    monkeypatch.setattr("infra.system_package._PACKAGE_DIR", package)
    return package


def test_resolved_system_row_uses_byte_equal_workspace_projection(
    copied_package: Path, tmp_path: Path
) -> None:
    workspace = tmp_path / "chosen-workspace"
    resolved = resolve_effective_skills(
        thread_id="t1", project_id=None, workspace=workspace, db=_db()
    )

    row = next(item for item in resolved.included if item.scope == "system")
    relative = row.path.relative_to(workspace / ".corsia")
    assert row.path.read_bytes() == (copied_package / relative).read_bytes()
    assert copied_package not in row.path.parents


def test_same_size_timestamp_preserved_tampering_is_restored(
    copied_package: Path, tmp_path: Path
) -> None:
    projection = ensure_system_projection(tmp_path)
    assert projection is not None
    target = projection / "skills" / "github" / "SKILL.md"
    source = (copied_package / "skills" / "github" / "SKILL.md").read_bytes()
    before = target.stat()
    target.chmod(0o644)
    target.write_bytes(b"x" * len(source))
    os.utime(target, ns=(before.st_atime_ns, before.st_mtime_ns))
    target.chmod(0o444)

    assert ensure_system_projection(tmp_path) == projection
    assert target.read_bytes() == source
    assert target.stat().st_mode & 0o777 == 0o444


def test_chmod_only_drift_is_restored(tmp_path: Path) -> None:
    projection = ensure_system_projection(tmp_path)
    assert projection is not None
    target = projection / "skills" / "github" / "SKILL.md"
    target.chmod(0o644)

    assert ensure_system_projection(tmp_path) == projection
    assert target.stat().st_mode & 0o777 == 0o444


def test_deleted_projected_file_is_restored(copied_package: Path, tmp_path: Path) -> None:
    projection = ensure_system_projection(tmp_path)
    assert projection is not None
    target = projection / "skills" / "github" / "SKILL.md"
    target.unlink()

    assert ensure_system_projection(tmp_path) == projection
    assert target.read_bytes() == (copied_package / "skills" / "github" / "SKILL.md").read_bytes()


def test_replaced_projected_file_is_repaired(copied_package: Path, tmp_path: Path) -> None:
    projection = ensure_system_projection(tmp_path)
    assert projection is not None
    target = projection / "skills" / "github" / "SKILL.md"
    replacement = tmp_path / "replacement"
    replacement.write_bytes(b"x" * target.stat().st_size)
    os.replace(replacement, target)

    assert ensure_system_projection(tmp_path) == projection
    assert target.read_bytes() == (copied_package / "skills" / "github" / "SKILL.md").read_bytes()
    assert target.stat().st_mode & 0o777 == 0o444


def test_warm_projection_uses_no_exclusive_lock(monkeypatch, tmp_path: Path) -> None:
    projection = ensure_system_projection(tmp_path)
    assert projection is not None
    calls = []
    original_flock = system_projection.fcntl.flock

    def record_lock(descriptor, operation):
        calls.append(operation)
        return original_flock(descriptor, operation)

    monkeypatch.setattr(system_projection.fcntl, "flock", record_lock)

    assert ensure_system_projection(tmp_path) == projection
    assert calls == []


def test_warm_projection_needs_no_hashing(monkeypatch, tmp_path: Path) -> None:
    projection = ensure_system_projection(tmp_path)
    assert projection is not None

    def forbid_hashing(*_args, **_kwargs):
        raise AssertionError("warm projection should not hash file content")

    monkeypatch.setattr(system_projection.hashlib, "sha256", forbid_hashing)

    assert ensure_system_projection(tmp_path) == projection


def test_warm_projection_calls_do_not_serialize_on_the_workspace_lock(monkeypatch, tmp_path: Path) -> None:
    projection = ensure_system_projection(tmp_path)
    assert projection is not None
    calls = []
    original_flock = system_projection.fcntl.flock

    def record_lock(descriptor, operation):
        calls.append(operation)
        return original_flock(descriptor, operation)

    monkeypatch.setattr(system_projection.fcntl, "flock", record_lock)
    with ThreadPoolExecutor(max_workers=4) as executor:
        results = list(executor.map(lambda _: ensure_system_projection(tmp_path), range(4)))

    assert results == [projection] * 4
    assert calls == []


def test_unowned_projected_surface_is_preserved(tmp_path: Path) -> None:
    user_skill = tmp_path / ".corsia" / "skills" / "user" / "SKILL.md"
    user_skill.parent.mkdir(parents=True)
    user_skill.write_text("user content", encoding="utf-8")

    assert ensure_system_projection(tmp_path) is None
    assert user_skill.read_text(encoding="utf-8") == "user content"
    assert not (tmp_path / ".corsia" / "MANIFEST.json").exists()


def test_manifest_drop_removes_stale_projected_file(copied_package: Path, tmp_path: Path) -> None:
    projection = ensure_system_projection(tmp_path)
    assert projection is not None
    stale = projection / "skills" / "github" / "SKILL.md"
    manifest_path = copied_package / "MANIFEST.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    manifest["files"] = [item for item in manifest["files"] if item["path"] != "skills/github/SKILL.md"]
    manifest_path.write_text(json.dumps(manifest), encoding="utf-8")

    assert ensure_system_projection(tmp_path) == projection
    assert not stale.exists()


def test_unwritable_projection_yields_unavailable_row(monkeypatch, tmp_path: Path) -> None:
    def refuse(*_args, **_kwargs):
        raise PermissionError("read-only workspace")

    monkeypatch.setattr("core.system_projection._atomic_write", refuse)
    resolved = resolve_effective_skills(
        thread_id="t1", project_id=None, workspace=tmp_path, db=_db()
    )

    assert resolved.system_unavailable == "System skills unavailable"
    assert not any(system_package_dir() in row.path.parents for row in resolved.all_rows())


def test_workspace_root_change_creates_a_new_projection(tmp_path: Path) -> None:
    first = ensure_system_projection(tmp_path / "one")
    second = ensure_system_projection(tmp_path / "two")

    assert first == tmp_path / "one" / ".corsia"
    assert second == tmp_path / "two" / ".corsia"
    assert (second / "skills" / "github" / "SKILL.md").is_file()


def test_generated_gitignore_is_exact(tmp_path: Path) -> None:
    projection = ensure_system_projection(tmp_path)

    assert projection is not None
    assert (projection / ".gitignore").read_text(encoding="utf-8") == (
        "skills/\ncontext/\ncoordination-models/\nMANIFEST.json\nREADME.md\n.gitignore\n"
    )
    manifest = json.loads((projection / "MANIFEST.json").read_text(encoding="utf-8"))
    skill = projection / manifest["files"][8]["path"]
    assert hashlib.sha256(skill.read_bytes()).hexdigest() == manifest["files"][8]["sha256"]


def test_coordination_models_project_from_their_one_source(tmp_path: Path) -> None:
    projection = ensure_system_projection(tmp_path)

    assert projection is not None
    index = projection / "coordination-models" / "INDEX.md"
    assert index.read_bytes() == (coordination_models_dir() / "INDEX.md").read_bytes()
    assert index.stat().st_mode & 0o777 == 0o444
    assert (projection / "coordination-models" / "corsia-default" / "manifest.yaml").is_file()


def test_tampered_coordination_model_is_restored(tmp_path: Path) -> None:
    projection = ensure_system_projection(tmp_path)
    assert projection is not None
    index = projection / "coordination-models" / "INDEX.md"
    index.chmod(0o644)
    index.write_text("hand edit", encoding="utf-8")

    assert ensure_system_projection(tmp_path) == projection
    assert index.read_bytes() == (coordination_models_dir() / "INDEX.md").read_bytes()
    assert index.stat().st_mode & 0o777 == 0o444


def test_undeclared_system_file_is_removed(tmp_path: Path) -> None:
    projection = ensure_system_projection(tmp_path)
    assert projection is not None
    stray = projection / "skills" / "stray" / "SKILL.md"
    stray.parent.mkdir()
    stray.write_text("hand-maintained copy", encoding="utf-8")

    assert ensure_system_projection(tmp_path) == projection
    assert not stray.exists()
    assert not stray.parent.exists()


def test_undeclared_coordination_model_file_is_removed(tmp_path: Path) -> None:
    projection = ensure_system_projection(tmp_path)
    assert projection is not None
    stray = projection / "coordination-models" / "stray" / "MODEL.md"
    stray.parent.mkdir()
    stray.write_text("hand-maintained copy", encoding="utf-8")

    assert ensure_system_projection(tmp_path) == projection
    assert not stray.exists()
    assert not stray.parent.exists()


@pytest.fixture
def copied_models(monkeypatch, tmp_path: Path) -> Path:
    models = tmp_path / "installed-models"
    shutil.copytree(coordination_models_dir(), models)
    monkeypatch.setattr("infra.system_package._COORDINATION_MODELS_DIR", models)
    return models


@pytest.mark.parametrize("breakage", ["missing", "mismatched"])
def test_unusable_coordination_model_drops_only_its_root(
    copied_models: Path, tmp_path: Path, caplog, breakage: str
) -> None:
    workspace = tmp_path / "ws"
    projection = ensure_system_projection(workspace)
    assert projection is not None
    source = copied_models / "INDEX.md"
    original = source.read_bytes()
    if breakage == "missing":
        source.unlink()
    else:
        source.write_bytes(original + b"\nEdited without regenerating.\n")

    with caplog.at_level("WARNING", logger="core.system_projection"):
        assert ensure_system_projection(workspace) == projection
        assert ensure_system_projection(workspace) == projection
    warnings = [r for r in caplog.records if "Coordination models not projected" in r.message]
    assert len(warnings) == 1
    assert not (projection / "coordination-models").exists()
    assert (projection / "skills" / "github" / "SKILL.md").is_file()
    assert any((projection / "context").iterdir())

    source.write_bytes(original)
    assert ensure_system_projection(workspace) == projection
    assert (projection / "coordination-models" / "INDEX.md").read_bytes() == original


def test_coordination_models_are_never_system_context(tmp_path: Path) -> None:
    from core.system_tier import system_tier_sources

    rows = system_tier_sources(("agent", "conductor"), workspace=tmp_path)

    assert rows
    assert not any("coordination-models" in row.source_id for row in rows)


def test_static_sandbox_allow_list_excludes_system_package() -> None:
    from tools.sandbox_config import _STATIC_ALLOWED_RO

    assert system_package_dir() not in _STATIC_ALLOWED_RO


def test_projection_is_advertised_only_inside_ordinary_read_scope(monkeypatch) -> None:
    """Governance #2648: a workspace under the state root is never advertised."""
    from infra.config import STATE_ROOT, WORKSPACE

    assert real_agent_readable(WORKSPACE / ".corsia")
    assert not real_agent_readable(STATE_ROOT / "ws" / ".corsia")

    monkeypatch.setattr("core.system_projection._agent_readable", real_agent_readable)
    assert ensure_system_projection(STATE_ROOT / "ws") is None
    assert not (STATE_ROOT / "ws" / ".corsia").exists()
