from __future__ import annotations

import asyncio
import os
import sqlite3
import subprocess
import sys
import threading
import time
from pathlib import Path
from types import SimpleNamespace

import pytest
import yaml

from core.skill_sidecar import installed_digest
from registry import models
from tests.event_routing_helpers import mk_session


@pytest.fixture(autouse=True)
def community_workspace(tmp_path, monkeypatch):
    from infra import config
    from infra.user_profile import PROFILE_RELATIVE_PATH, USE_COMMUNITY_LABEL

    profile = tmp_path / PROFILE_RELATIVE_PATH
    profile.parent.mkdir()
    profile.write_text(f"{USE_COMMUNITY_LABEL} Yes\n", encoding="utf-8")
    monkeypatch.setattr(config, "WORKSPACE", tmp_path)
    return tmp_path


def _item(*, withdrawn: bool = False) -> dict:
    return {
        "name": "pdf",
        "description": "PDF work.",
        "version": "2.0.0",
        "license": "Apache-2.0",
        "source": {"repository": "openai/skills", "commit": "b" * 40, "path": "skills/pdf"},
        "files": [{"path": "SKILL.md", "sha256": "sha256:" + "a" * 64}],
        "withdrawn": withdrawn,
        "withdrawn_reason": "unsafe" if withdrawn else None,
    }


def _copy(folder, *, version="1.0.0"):
    folder.mkdir(parents=True)
    skill = b"---\nname: pdf\ndescription: PDF work\n---\nUse it.\n"
    origin = {
        "source": "openai/skills@" + "a" * 40 + ":skills/pdf",
        "sha256": "a" * 64,
        "version": version,
        "adopted_at": "2026-10-19T00:00:00+00:00",
    }
    files = {"SKILL.md": skill, "corsia.yaml": yaml.safe_dump({"origin": origin}).encode()}
    origin["installed_digest"] = installed_digest(files)
    (folder / "SKILL.md").write_bytes(skill)
    (folder / "corsia.yaml").write_text(yaml.safe_dump({"origin": origin}))


def _count(sql: str, *args) -> int:
    from infra.database import get_db

    db = get_db()
    try:
        return db.execute(sql, args).fetchone()[0]
    finally:
        db.close()


def test_runtime_lifecycle_tracks_the_daily_library_refresh_task():
    from app_startup_runtime import RUNTIME_LOOP_TASKS
    from infra import state

    assert ("library refresh", "_library_refresh_task") in RUNTIME_LOOP_TASKS
    assert hasattr(state, "_library_refresh_task")


def test_producer_handles_1000_copies_under_five_seconds_on_an_indexed_query(isolated_db, tmp_path, monkeypatch):
    """The daily job scales with library copies, never the routing-registration table."""
    from infra.database import get_db
    from library import producer

    owner = mk_session("library-scale-owner")
    items = []
    for number in range(1000):
        name = f"pdf-{number}"
        folder = tmp_path / "skills" / name
        _copy(folder)
        models.register_artifact(
            integration="library", type="installed-skill", external_id=f"{owner}/{name}",
            parent_thread_id=owner, path=str(folder / "SKILL.md"),
        )
        items.append({**_item(), "name": name})
    monkeypatch.setattr("library.load_index", lambda: SimpleNamespace(items=tuple(items), index_age="just now", generations={}))
    monkeypatch.setattr("events.platform.deliver_platform_event_sync", lambda *_args: [])

    started = time.perf_counter()
    assert producer.produce_once() == 0
    elapsed = time.perf_counter() - started

    db = get_db()
    try:
        plan = db.execute(
            "EXPLAIN QUERY PLAN " + producer.INSTALLED_ROWS_SQL,
            ("library", "installed-skill"),
        ).fetchall()
    finally:
        db.close()
    details = " ".join(row[3] for row in plan)
    assert elapsed < 5
    assert "USING INDEX" in details
    assert "SCAN artifact_registrations" not in details


def test_producer_reads_installed_artifacts_and_delivers_each_change_once(isolated_db, tmp_path, monkeypatch):
    from library import producer

    owner = mk_session("library-owner")
    skill = tmp_path / "skills" / "pdf"
    _copy(skill)
    models.register_artifact(
        integration="library", type="installed-skill", external_id=f"{owner}/pdf",
        parent_thread_id=owner, path=str(skill / "SKILL.md"),
    )
    monkeypatch.setattr("library.load_index", lambda: SimpleNamespace(items=(_item(),), index_age="just now", generations={}))

    first = producer.produce_once()
    repeat = producer.produce_once()

    assert (first, repeat) == (1, 0)
    assert _count("SELECT COUNT(*) FROM context_change_journal WHERE object_id=?", owner) == 1


def test_producer_skips_change_notices_without_consent_but_sends_withdrawals(
    isolated_db, tmp_path, monkeypatch, community_workspace,
):
    from infra.user_profile import PROFILE_RELATIVE_PATH, USE_COMMUNITY_LABEL
    from library import producer

    owner = mk_session("library-owner")
    skill = tmp_path / "skills" / "pdf"
    _copy(skill)
    models.register_artifact(
        integration="library", type="installed-skill", external_id=f"{owner}/pdf",
        parent_thread_id=owner, path=str(skill / "SKILL.md"),
    )
    profile = community_workspace / PROFILE_RELATIVE_PATH
    profile.write_text(f"{USE_COMMUNITY_LABEL} No\n", encoding="utf-8")
    monkeypatch.setattr(
        "library.load_index", lambda: SimpleNamespace(items=(_item(),), index_age="just now", generations={}),
    )

    assert producer.produce_once() == 0
    assert _count("SELECT COUNT(*) FROM messages WHERE thread_id=? AND id LIKE 'platform-event:%'", owner) == 0

    monkeypatch.setattr(
        "library.load_index", lambda: SimpleNamespace(
            items=(_item(withdrawn=True),), index_age="just now", generations={},
        ),
    )

    assert producer.produce_once() == 1
    assert _count("SELECT COUNT(*) FROM messages WHERE thread_id=? AND id LIKE 'platform-event:%'", owner) == 1


def test_producer_notices_a_version_change_at_the_same_source(isolated_db, tmp_path):
    from library import producer
    from library.items import item_manifest, source_ref

    owner = mk_session("library-owner")
    skill = tmp_path / "skills" / "pdf"
    _copy(skill)
    current = _item()
    current["source"]["commit"] = "a" * 40
    prior = {**current, "version": "1.0.0"}
    sidecar = yaml.safe_load((skill / "corsia.yaml").read_text())
    sidecar["origin"]["source"] = source_ref(current)
    sidecar["origin"]["item_manifest"] = item_manifest(prior)
    (skill / "corsia.yaml").write_text(yaml.safe_dump(sidecar))
    row = {"external_id": f"{owner}/pdf", "path": str(skill / "SKILL.md")}

    payload = producer._notice_payload(row, {"pdf": current}, {})

    assert payload is not None
    assert payload["event_type"] == "library.item_changed"
    assert payload["installed_version"] == "1.0.0"
    assert payload["library_version"] == "2.0.0"


def test_withdrawal_leaves_an_installed_copy_current():
    """Withdrawal is announced by its own notice; it never makes a copy look out of date."""
    from library.items import item_manifest, source_ref
    from tools.skill_library_common import current_library_item

    origin = {"source": source_ref(_item()), "item_manifest": item_manifest(_item())}

    assert current_library_item(origin, _item(withdrawn=True))


def test_producer_delivers_withdrawal_again_after_a_reinstatement(isolated_db, tmp_path, monkeypatch):
    from library import producer

    owner = mk_session("library-owner")
    skill = tmp_path / "skills" / "pdf"
    _copy(skill)
    models.register_artifact(
        integration="library", type="installed-skill", external_id=f"{owner}/pdf",
        parent_thread_id=owner, path=str(skill / "SKILL.md"),
    )
    from library.items import item_manifest, source_ref

    reinstated = _item()
    sidecar = yaml.safe_load((skill / "corsia.yaml").read_text())
    sidecar["origin"].update({"source": source_ref(reinstated), "item_manifest": item_manifest(reinstated)})
    (skill / "corsia.yaml").write_text(yaml.safe_dump(sidecar))
    states = iter((_item(withdrawn=True), reinstated, _item(withdrawn=True)))
    generations = iter((1, 2, 3))
    monkeypatch.setattr(
        "library.load_index",
        lambda: SimpleNamespace(items=(next(states),), index_age="just now", generations={"pdf": next(generations)}),
    )

    assert producer.produce_once() == 1
    assert producer.produce_once() == 0
    assert producer.produce_once() == 1
    assert _count("SELECT COUNT(*) FROM messages WHERE thread_id=? AND id LIKE 'platform-event:%'", owner) == 2


def test_producer_withdrawal_wakes_the_registered_owner_once(isolated_db, tmp_path, monkeypatch):
    from library import producer

    owner = mk_session("library-owner")
    skill = tmp_path / "skills" / "pdf"
    _copy(skill)
    models.register_artifact(
        integration="library", type="installed-skill", external_id=f"{owner}/pdf",
        parent_thread_id=owner, path=str(skill / "SKILL.md"),
    )
    monkeypatch.setattr("library.load_index", lambda: SimpleNamespace(items=(_item(withdrawn=True),), index_age="just now", generations={}))

    producer.produce_once()
    producer.produce_once()

    assert _count("SELECT COUNT(*) FROM messages WHERE thread_id=? AND id LIKE 'platform-event:%'", owner) == 1


_CHILD_ENTRYPOINT = "CORSIA_LIBRARY_PRODUCER_CHILD_ENTRYPOINT"
_CHILD_TIMEOUT_SECONDS = 12


def _run_pool_lifetime_child(entrypoint: str, tmp_path: Path) -> subprocess.CompletedProcess[str]:
    workspace = tmp_path / "workspace"
    state = tmp_path / "state"
    workspace.mkdir()
    state.mkdir()
    environment = {
        **os.environ,
        _CHILD_ENTRYPOINT: entrypoint,
        "CORSIA_WORKSPACE": str(workspace),
        "CORSIA_STATE_DIR": str(state),
        "CORSIA_TEST_MODE": "1",
        "CORSIA_ANALYTICS_DISABLE": "1",
    }
    try:
        return subprocess.run(
            [sys.executable, "-m", "pytest", str(Path(__file__)), "-q", "-k", "pool_lifetime_child"],
            env=environment, capture_output=True, text=True, timeout=_CHILD_TIMEOUT_SECONDS,
        )
    except subprocess.TimeoutExpired as error:
        pytest.fail(f"{entrypoint} producer child exceeded its {_CHILD_TIMEOUT_SECONDS}s hard timeout: {error}")


@pytest.mark.parametrize("entrypoint", ("daily", "requested"))
def test_producer_cancellation_joins_real_pool_query_before_shutdown(tmp_path, entrypoint):
    """The child kill bound leaves no stuck SQLite worker in the main test runner."""
    result = _run_pool_lifetime_child(entrypoint, tmp_path)
    assert result.returncode == 0, result.stdout + result.stderr


async def _wait_for(event: threading.Event, *, timeout: float = 5) -> None:
    deadline = time.monotonic() + timeout
    while not event.is_set():
        if time.monotonic() >= deadline:
            pytest.fail(f"event did not arrive within {timeout}s")
        await asyncio.sleep(0.01)


async def _producer_task(entrypoint: str):
    from infra import app_loop
    from library import producer
    from ws import ingress

    if entrypoint == "daily":
        return asyncio.create_task(producer.library_refresh_loop()), False
    app_loop.capture(asyncio.get_running_loop())
    producer.request_refresh()
    while not ingress.background_tasks:
        await asyncio.sleep(0)
    return next(iter(ingress.background_tasks)), True


@pytest.mark.asyncio
async def test_producer_pool_lifetime_child(isolated_db, monkeypatch):
    """The child process runs one real pooled query, cancellation, and pool shutdown."""
    entrypoint = os.environ.get(_CHILD_ENTRYPOINT)
    if entrypoint not in {"daily", "requested"}:
        pytest.skip("only run through the bounded parent regression")
    from infra import app_loop, database
    from library import producer

    query_started = threading.Event()
    release_query = threading.Event()
    producer_finished = threading.Event()
    reconfigured = threading.Event()
    real_connect = database.sqlite3.connect

    def connect(*args, **kwargs):
        connection = real_connect(*args, **kwargs)

        def authorize(action, table, *_args):
            if action == sqlite3.SQLITE_READ and table == "artifact_identities":
                query_started.set()
                if not release_query.wait(timeout=5):
                    return sqlite3.SQLITE_DENY
            return sqlite3.SQLITE_OK

        connection.set_authorizer(authorize)
        return connection

    real_produce_once = producer.produce_once

    def produce_once():
        try:
            return real_produce_once()
        finally:
            producer_finished.set()

    def reconfigure():
        assert producer_finished.is_set()
        reconfigured.set()

    monkeypatch.setattr(database.sqlite3, "connect", connect)
    monkeypatch.setattr(producer, "produce_once", produce_once)
    monkeypatch.setattr("library.load_index", lambda: SimpleNamespace(items=(), index_age="just now", generations={}))
    task, clear_loop = await _producer_task(entrypoint)
    shutdown = threading.Thread(target=database.shutdown_pool, args=(reconfigure,), name="producer-pool-shutdown")
    try:
        await _wait_for(query_started)
        shutdown.start()
        task.cancel()
        await asyncio.sleep(0)
        assert not task.done()
        release_query.set()
        with pytest.raises(asyncio.CancelledError):
            await asyncio.wait_for(task, timeout=5)
        await _wait_for(reconfigured)
        shutdown.join(timeout=1)
        assert not shutdown.is_alive()
    finally:
        release_query.set()
        if not task.done():
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
        if shutdown.is_alive():
            shutdown.join(timeout=1)
        if clear_loop:
            app_loop.clear()
