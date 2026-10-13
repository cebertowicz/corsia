from __future__ import annotations

import asyncio
import json
import os
import sqlite3
import subprocess
from datetime import datetime, timedelta, timezone
from pathlib import Path


def _git(cwd: Path, *args: str, env: dict | None = None) -> str:
    result = subprocess.run(
        ["git", *args], cwd=cwd, text=True, capture_output=True, check=True, env=env
    )
    return result.stdout.strip()


def _repo_with_remote(tmp_path: Path, branch: str) -> tuple[Path, Path]:
    seed = tmp_path / "seed"
    remote = tmp_path / "origin.git"
    clone = tmp_path / "clone"
    _git(tmp_path, "init", "--bare", str(remote))
    _git(tmp_path, "init", "--initial-branch", branch, str(seed))
    _git(seed, "config", "user.email", "test@example.com")
    _git(seed, "config", "user.name", "Test")
    (seed / "CHANGELOG.md").write_text("## v1.0.0 — First\n\n### Added\n- First item\n")
    _git(seed, "add", "CHANGELOG.md")
    _git(seed, "commit", "-m", "initial")
    _git(seed, "tag", "v1.0.0")
    _git(seed, "remote", "add", "origin", str(remote))
    _git(seed, "push", "--tags", "origin", branch)
    _git(tmp_path, "clone", str(remote), str(clone))
    _git(clone, "checkout", branch)
    return seed, clone


def _real_git(repo: Path):
    def run(args: list[str]) -> str:
        return _git(repo, *args)

    return run


def test_published_details_parse_changelog_and_cap_items(tmp_path):
    from infra.update_changes import build_update_details

    seed, clone = _repo_with_remote(tmp_path, "main")
    items = "\n".join(f"- Item {index}" for index in range(51))
    (seed / "CHANGELOG.md").write_text(f"## v1.1.0 — Useful release\n\n### Added\n{items}\n")
    _git(seed, "add", "CHANGELOG.md")
    _git(seed, "commit", "-m", "release notes")
    _git(seed, "tag", "v1.1.0")
    _git(seed, "push", "--tags", "origin", "main")
    _git(clone, "fetch", "origin", "main")

    details = build_update_details(True, "main", _real_git(clone))

    assert details.current_version == "1.0.0"
    assert details.target_version == "1.1.0"
    assert details.changes == {
        "kind": "changelog",
        "headline": "Useful release",
        "items": [f"Item {index}" for index in range(50)],
        "more": 1,
    }


def test_development_details_drop_merge_subjects_and_disambiguate_dates(tmp_path):
    from infra.update_changes import build_update_details

    seed, clone = _repo_with_remote(tmp_path, "dev")
    date = "2026-10-13T12:00:00+0000"
    env = {**os.environ, "GIT_AUTHOR_DATE": date, "GIT_COMMITTER_DATE": date}
    (seed / "note.txt").write_text("next")
    _git(seed, "add", "note.txt")
    _git(seed, "commit", "-m", "Useful change", env=env)
    _git(seed, "commit", "--allow-empty", "-m", "Merge branch 'other'", env=env)
    _git(seed, "push", "origin", "dev")
    _git(clone, "fetch", "origin", "dev")

    details = build_update_details(True, "dev", _real_git(clone))

    assert details.current_version.startswith("2026.10.13.")
    assert details.target_version == "2026.10.13.1200"
    assert details.current_version != details.target_version
    assert details.changes == {
        "kind": "commits",
        "headline": None,
        "items": ["Useful change"],
        "more": 0,
    }
    assert build_update_details(False, "dev", _real_git(clone)).target_version is None


def test_usual_update_seconds_uses_ten_newest_completed_rows():
    from automation.self_restart import usual_update_seconds

    db = sqlite3.connect(":memory:")
    db.row_factory = sqlite3.Row
    db.execute(
        "CREATE TABLE self_restart_requests "
        "(kind TEXT, status TEXT, requested_at TEXT, settled_at TEXT, id TEXT)"
    )
    now = datetime.now(timezone.utc)
    for index in range(12):
        requested = now - timedelta(minutes=index)
        seconds = 1000 if index >= 10 else (6 if index in {4, 6} else index + 1)
        settled = requested + timedelta(seconds=seconds)
        db.execute(
            "INSERT INTO self_restart_requests VALUES (?,?,?,?,?)",
            ("update", "complete", requested.isoformat(), settled.isoformat(), str(index)),
        )
    db.execute(
        "INSERT INTO self_restart_requests VALUES (?,?,?,?,?)",
        ("update", "failed", now.isoformat(), now.isoformat(), "failed"),
    )
    db.commit()

    assert usual_update_seconds(db=db) == 6
    empty = sqlite3.connect(":memory:")
    empty.row_factory = sqlite3.Row
    empty.execute(
        "CREATE TABLE self_restart_requests "
        "(kind TEXT, status TEXT, requested_at TEXT, settled_at TEXT, id TEXT)"
    )
    assert usual_update_seconds(db=empty) is None


def test_operation_projection_limits_logs_and_reports_live_blocking(tmp_path, monkeypatch):
    from infra import config, drain
    from infra.update_operation import project_update_operation

    logs = tmp_path / "logs"
    logs.mkdir()
    log = logs / "update.log"
    log.write_text("x" * 6001)
    monkeypatch.setattr(config, "LOGS_DIR", logs)
    monkeypatch.setattr(
        drain,
        "get_drain_status",
        lambda: {"activeTurns": 3, "hard_cap_remaining": 287},
    )
    operation = {
        "id": "operation",
        "status": "in_flight",
        "phase": "draining",
        "kind": "update",
        "trigger": "manual",
    }
    active = project_update_operation(operation)
    assert active["blocking"] == 3
    assert active["hard_cap_remaining"] == 287

    operation.update(
        status="failed",
        failure_detail=json.dumps({"log_path": str(log)}),
    )
    result = project_update_operation(operation)
    assert result["kind"] == "update"
    assert result["trigger"] == "manual"
    assert result["blocking"] is None
    assert result["hard_cap_remaining"] is None
    assert result["log_tail"] == "x" * 6000
    operation["failure_detail"] = json.dumps({"log_path": str(tmp_path / "outside.log")})
    assert project_update_operation(operation)["log_tail"] is None


def test_update_status_route_reads_live_drain_cap(monkeypatch):
    from api.system_admin import system_update_status
    from automation import self_restart
    from infra import db_async, drain, update_status

    class Status:
        def to_dict(self):
            return {"available": False}

    async def run_in_db(function):
        if function is self_restart.usual_update_seconds:
            return None
        if function is self_restart.latest_update_operation:
            return None
        raise AssertionError(f"unexpected database function: {function}")

    monkeypatch.setattr(update_status, "update_status", lambda refresh: Status())
    monkeypatch.setattr(db_async, "run_in_db", run_in_db)
    monkeypatch.setattr(drain, "DRAIN_HARD_CAP", 17)

    result = asyncio.run(system_update_status(user={}))

    assert result["drain_cap_seconds"] == 17
    assert result["usual_seconds"] is None
    assert datetime.fromisoformat(result["server_time"]).tzinfo == timezone.utc


def test_settings_update_acquire_uses_cached_target_version(monkeypatch):
    from api.settings_restart import _acquire_settings_restart
    from automation import self_restart
    from infra import db_async, update_status

    captured: list[dict] = []

    async def run_in_db(function):
        return function()

    def acquire(*args, **kwargs):
        captured.append(kwargs)
        return "operation-id"

    monkeypatch.setattr(db_async, "run_in_db", run_in_db)
    monkeypatch.setattr(self_restart, "try_acquire", acquire)
    monkeypatch.setattr(update_status, "cached_target_version", lambda: "1.2.3")

    assert asyncio.run(_acquire_settings_restart({"id": "user"}, "update", [])) == "operation-id"
    assert captured[-1]["target_version"] == "1.2.3"
    assert asyncio.run(_acquire_settings_restart({"id": "user"}, "restart", [])) == "operation-id"
    assert captured[-1]["target_version"] is None
