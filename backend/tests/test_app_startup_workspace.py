from __future__ import annotations

from types import SimpleNamespace


def _quiet_startup(monkeypatch, calls: list) -> None:
    from core import skill_data_conversion, skill_key_conversion, system_projection
    from infra import guidance_git
    from registry import shared_ownership_assignment

    monkeypatch.setattr(
        system_projection, "ensure_system_projection", lambda path: calls.append(("projection", path)),
    )
    monkeypatch.setattr(
        skill_key_conversion, "convert_legacy_skill_keys", lambda path: calls.append(("skill_keys", path)),
    )
    monkeypatch.setattr(
        skill_data_conversion, "convert_legacy_skill_data", lambda path: calls.append(("skill_data", path)),
    )
    monkeypatch.setattr(
        guidance_git, "startup_sweep", lambda path: calls.append(("sweep", path)),
    )
    monkeypatch.setattr(
        shared_ownership_assignment,
        "run_startup_assignment",
        lambda path: calls.append(("assignment", path)),
    )


def test_workspace_startup_runs_projection_skill_keys_skill_data_sweep_then_assignment(tmp_path, monkeypatch):
    from app_startup_workspace import initialize_workspace

    calls: list = []
    _quiet_startup(monkeypatch, calls)
    initialize_workspace(tmp_path, SimpleNamespace(exception=lambda *_a: None))
    assert calls == [
        ("projection", tmp_path),
        ("skill_keys", tmp_path),
        ("skill_data", tmp_path),
        ("sweep", tmp_path),
        ("assignment", tmp_path),
    ]


def test_workspace_startup_logs_skill_key_failure_and_still_sweeps(tmp_path, monkeypatch):
    from core import skill_key_conversion
    from app_startup_workspace import initialize_workspace

    calls: list = []
    messages: list = []
    _quiet_startup(monkeypatch, calls)

    def fail(_path):
        raise RuntimeError("conversion failed")

    monkeypatch.setattr(skill_key_conversion, "convert_legacy_skill_keys", fail)
    initialize_workspace(tmp_path, SimpleNamespace(exception=messages.append))
    assert messages == ["skill frontmatter key conversion failed; retrying next start"]
    assert [name for name, _path in calls] == ["projection", "skill_data", "sweep", "assignment"]


def test_workspace_startup_logs_skill_data_failure_and_still_sweeps(tmp_path, monkeypatch):
    from core import skill_data_conversion
    from app_startup_workspace import initialize_workspace

    calls: list = []
    messages: list = []
    _quiet_startup(monkeypatch, calls)

    def fail(_path):
        raise RuntimeError("conversion failed")

    monkeypatch.setattr(skill_data_conversion, "convert_legacy_skill_data", fail)
    initialize_workspace(tmp_path, SimpleNamespace(exception=messages.append))
    assert messages == ["skill CORSIA data conversion failed; retrying next start"]
    assert [name for name, _path in calls] == ["projection", "skill_keys", "sweep", "assignment"]


def test_workspace_startup_logs_assignment_failure_without_blocking(tmp_path, monkeypatch):
    from registry import shared_ownership_assignment
    from app_startup_workspace import initialize_workspace

    messages = []
    _quiet_startup(monkeypatch, [])

    def fail(_path):
        raise RuntimeError("assignment failed")

    monkeypatch.setattr(shared_ownership_assignment, "run_startup_assignment", fail)
    initialize_workspace(tmp_path, SimpleNamespace(exception=messages.append))
    assert messages == [
        "shared ownership assignment startup pass failed; retrying next start"
    ]


def test_workspace_startup_runs_the_pane_row_passes_in_rule_order(tmp_path, monkeypatch):
    from app_startup_workspace import initialize_workspace
    from registry import (
        finished_task_row_backfill,
        ownership_row_backfill,
        taskless_row_backfill,
    )

    calls: list = []
    _quiet_startup(monkeypatch, calls)
    monkeypatch.setattr(
        ownership_row_backfill, "run_startup_backfill",
        lambda: calls.append(("ownership_rows", None)),
    )
    monkeypatch.setattr(
        finished_task_row_backfill, "run_startup_backfill",
        lambda: calls.append(("finished_task_rows", None)),
    )
    monkeypatch.setattr(
        taskless_row_backfill, "run_startup_backfill",
        lambda: calls.append(("taskless_rows", None)),
    )
    initialize_workspace(tmp_path, SimpleNamespace(exception=lambda *_a: None))
    assert [name for name, _path in calls][-4:] == [
        "assignment", "ownership_rows", "finished_task_rows", "taskless_rows",
    ]
