"""Inherited files are visible without widening evidence identity or writing projections."""
from __future__ import annotations

from contextlib import contextmanager
import json
import sqlite3

import pytest

from core.lesson_files import render_trial
from core.lesson_trials import Trial
from tests.test_lesson_report_adapters import report_target  # noqa: F401

SUBJECT = "6680cac5-3aa2-4a88-b491-352459567323"


def write_trial(path):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(render_trial(lesson_id=SUBJECT, owner="owner", name="inherited",
                    description="Use when checking inherited guidance.", text="Check the actual receiver.",
                    trial=Trial("hypothesis", "2020-01-01"),
                    source_refs=[{"kind": "message", "reference": "fixture"}]))


@pytest.mark.asyncio
async def test_self_reads_same_user_ancestor_and_role_context_without_materializing(report_target, monkeypatch):
    from core import system_projection
    from core.skill_resolution import project_dir, thread_dir

    db, _app, tool = report_target
    project_root = tool.workspace / "projects" / "project"
    project_root.mkdir(parents=True)
    db.execute("UPDATE projects SET path=? WHERE id='project'", (str(project_root),))
    db.commit()
    planning = project_dir("project", tool.workspace, db)
    assert planning == project_root
    parent = thread_dir("owner", project_dir=planning, workspace=tool.workspace)
    write_trial(parent / "skills" / "inherited" / "SKILL.md")
    role_file = tool.workspace / "context" / "roles" / "agent" / "retained.md"
    role_file.parent.mkdir(parents=True)
    role_file.write_text("# Agent context\nUse current source evidence.\n")

    def forbidden(*args, **kwargs):
        pytest.fail("read-only report reached System materialization")

    monkeypatch.setattr(system_projection, "ensure_system_projection", forbidden)
    result = json.loads(await tool.execute(_thread_id="child", lesson_id=SUBJECT))
    assert result["detail"]["id"] == SUBJECT
    assert result["detail"]["source_scope"]["scope_id"] == "owner"
    assert result["scope"] == "self:child"
    discovery = result["coverage"]["source_discovery"]
    assert discovery["role_context_files"] == 1
    assert discovery["role_skill_rows"] is None
    assert "3364" in discovery["limitations_issue"]
    assert discovery["system"]["packaged_version"] is not None
    assert discovery["system"]["projection_status"] is None
    assert discovery["current_files_are_delivery"] is False
    assert not (tool.workspace / ".corsia").exists()


@pytest.mark.asyncio
async def test_foreign_ancestry_never_grants_current_file_visibility(report_target):
    from core.skill_resolution import project_dir, thread_dir

    db, _app, tool = report_target
    project_root = tool.workspace / "projects" / "project"
    project_root.mkdir(parents=True)
    db.execute("UPDATE projects SET path=? WHERE id='project'", (str(project_root),))
    db.commit()
    planning = project_dir("project", tool.workspace, db)
    assert planning == project_root
    write_trial(thread_dir("foreign", project_dir=planning, workspace=tool.workspace) / "skills" / "inherited" / "SKILL.md")
    db.execute("UPDATE threads SET parent_thread_id='foreign' WHERE id='child'")
    db.commit()
    result = json.loads(await tool.execute(_thread_id="child", lesson_id=SUBJECT))
    assert result["detail"] is None


@pytest.mark.asyncio
async def test_unresolved_project_names_missing_roots_without_workspace_fallback(report_target, monkeypatch):
    from core import lesson_report_sources
    from core.skill_resolution import thread_dir

    _db, _app, tool = report_target
    write_trial(thread_dir("child", project_dir=None, workspace=tool.workspace) / "skills" / "inherited" / "SKILL.md")
    monkeypatch.setattr(
        lesson_report_sources,
        "resolve_project_row_paths",
        lambda *_args, **_kwargs: (None, None),
    )
    result = json.loads(await tool.execute(_thread_id="child", lesson_id=SUBJECT))
    assert result["detail"] is None
    assert {"path": "thread://child", "reason": "project planning root unavailable; selected thread and ancestor file roots unavailable"} in result["coverage"]["unavailable_sources"]


@pytest.mark.asyncio
async def test_missing_role_metadata_is_unavailable_instead_of_a_server_error(report_target, monkeypatch):
    from types import SimpleNamespace
    from core import lesson_report_sources

    _db, _app, tool = report_target
    source = SimpleNamespace(inclusion="included", stable_reference=str(tool.workspace / "role.md"), metadata={})
    monkeypatch.setattr(lesson_report_sources, "role_context_sources", lambda *_args, **_kwargs: (source,))
    result = json.loads(await tool.execute(_thread_id="child"))
    assert any(row["reason"] == "role source scope metadata unavailable" for row in result["coverage"]["unavailable_sources"])


def test_batched_source_fact_failure_is_not_silently_reclassified(report_target, monkeypatch):
    from core.lesson_report import ReportScope
    from core import lesson_report_sources

    db, _app, tool = report_target

    def unavailable(*_args, **_kwargs):
        raise sqlite3.OperationalError("injected source facts failure")

    monkeypatch.setattr(lesson_report_sources, "_source_rows", unavailable)
    with pytest.raises(sqlite3.OperationalError, match="injected source facts failure"):
        lesson_report_sources.source_inputs(
            db, tool.workspace,
            ReportScope("user", ("child",), "test", "", "9999-12-31", True),
        )


def test_source_inputs_batch_thread_project_ancestry_and_role_facts(report_target):
    from core.lesson_report import ReportScope
    from core.lesson_report_sources import source_inputs

    db, _app, tool = report_target
    project_root = tool.workspace / "projects" / "project"
    project_root.mkdir(parents=True)
    db.execute("UPDATE projects SET path=? WHERE id='project'", (str(project_root),))
    db.commit()
    traced = []
    db.set_trace_callback(traced.append)
    roots, roles, unavailable = source_inputs(
        db,
        tool.workspace,
        ReportScope("user", ("owner", "child"), "test", "", "9999-12-31", True),
    )
    db.set_trace_callback(None)

    source_queries = [statement for statement in traced if "WITH RECURSIVE" in statement]
    assert len(source_queries) == 1
    assert "root_is_project_conductor" in source_queries[0]
    assert not unavailable
    assert [thread for thread, _roles, _roots in roles] == ["owner", "child"]
    assert all(selected_roles == ("agent",) for _thread, selected_roles, _roots in roles)
    root_scopes = [scope for path, scope in roots.items() if path.name in {"owner", "child"}]
    assert {scope["scope_id"] for scope in root_scopes} == {"owner", "child"}


def test_source_inputs_preserve_project_conductor_role(report_target):
    from core.lesson_report import ReportScope
    from core.lesson_report_sources import source_inputs

    db, _app, tool = report_target
    db.execute("UPDATE projects SET conductor_thread_id='child' WHERE id='project'")
    db.commit()
    _roots, roles, _unavailable = source_inputs(
        db,
        tool.workspace,
        ReportScope("user", ("child",), "test", "", "9999-12-31", True),
    )
    assert roles[0][1] == ("agent", "conductor")


@pytest.mark.asyncio
async def test_non_agent_empty_roles_are_known_empty_not_a_resolution_failure(report_target):
    db, _app, tool = report_target
    db.execute("UPDATE threads SET agent_enabled=0 WHERE id='child'")
    db.commit()
    result = json.loads(await tool.execute(_thread_id="child"))
    assert result["coverage"]["source_discovery"]["role_context_files"] == 0
    assert all(row["path"] != "thread://child/roles" for row in result["coverage"]["unavailable_sources"])


@pytest.mark.asyncio
async def test_directory_scan_runs_after_read_snapshot_closes(report_target, monkeypatch):
    from core import lesson_report_access
    from core.lesson_report_db import report_snapshot

    _db, _app, tool = report_target
    snapshot_closed = False
    scan = lesson_report_access.current_sources

    @contextmanager
    def observed_snapshot():
        nonlocal snapshot_closed
        with report_snapshot() as snapshot:
            yield snapshot
        # Prove closure on its creating DB worker, not by a cross-thread call.
        with pytest.raises(sqlite3.ProgrammingError, match="closed"):
            snapshot.execute("SELECT 1")
        snapshot_closed = True

    def scan_after_close(*args):
        assert snapshot_closed
        return scan(*args)

    monkeypatch.setattr(lesson_report_access, "report_snapshot", observed_snapshot)
    monkeypatch.setattr(lesson_report_access, "current_sources", scan_after_close)
    result = json.loads(await tool.execute(_thread_id="child"))
    assert result["scope"] == "self:child"


def test_source_inputs_resolve_a_shared_project_and_each_thread_directory_once(report_target, monkeypatch):
    from core import lesson_report_sources
    from core.lesson_report import ReportScope

    db, _app, tool = report_target
    project_root = tool.workspace / "projects" / "project"
    project_root.mkdir(parents=True)
    db.execute("UPDATE projects SET path=? WHERE id='project'", (str(project_root),))
    db.commit()
    project_resolutions = []
    resolve = lesson_report_sources.resolve_project_row_paths
    monkeypatch.setattr(
        lesson_report_sources, "resolve_project_row_paths",
        lambda *args, **kwargs: project_resolutions.append(kwargs["project_name"]) or resolve(*args, **kwargs),
    )

    roots, roles, _unavailable = lesson_report_sources.source_inputs(
        db, tool.workspace, ReportScope("user", ("owner", "child"), "test", "", "9999-12-31", True),
    )

    assert len(project_resolutions) == 1
    planning = project_root.resolve()
    assert {path: scope["scope_id"] for path, scope in roots.items() if scope["scope"] == "thread"} == {
        planning / ".threads" / "owner": "owner",
        planning / ".threads" / "child": "child",
    }
    assert [role_roots[1].root for _thread, _selected, role_roots in roles] == [planning, planning]
