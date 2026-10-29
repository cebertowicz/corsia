from __future__ import annotations

import asyncio
import sqlite3
import threading

import pytest


class _CommitFailure:
    def __init__(self, connection) -> None:
        self.connection = connection
        self.closed = False

    def execute(self, *args, **kwargs):
        return self.connection.execute(*args, **kwargs)

    def commit(self) -> None:
        raise sqlite3.OperationalError("forced commit failure")

    @property
    def in_transaction(self):
        return self.connection.in_transaction

    def rollback(self) -> None:
        self.connection.rollback()

    def close(self) -> None:
        self.closed = True
        self.connection.close()


# A hang guard, not a timing assertion: opening a worker's first connection and
# committing to a fresh file can take over a second on a loaded host.
_START_WAIT_SECONDS = 30


async def _wait_started(started: threading.Event) -> None:
    assert await asyncio.to_thread(started.wait, _START_WAIT_SECONDS)


def _assert_writer_available(db_path) -> None:
    external = sqlite3.connect(str(db_path), timeout=0.05)
    try:
        external.execute("BEGIN IMMEDIATE")
        external.rollback()
    finally:
        external.close()


def _row_count(db_path, table: str) -> int:
    external = sqlite3.connect(str(db_path), timeout=0.05)
    try:
        return external.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
    finally:
        external.close()


@pytest.mark.asyncio
async def test_pool_close_excludes_queued_db_work_until_connections_are_closed() -> None:
    from infra import db_async
    from infra.db_async import run_in_db

    started = threading.Event()
    release = threading.Event()
    closed = threading.Event()
    events: list[str] = []

    def blocking_job() -> None:
        started.set()
        release.wait()

    first = asyncio.create_task(run_in_db(blocking_job))
    try:
        await _wait_started(started)
        def close() -> None:
            events.append("closed")
            closed.set()

        closing = asyncio.create_task(asyncio.to_thread(db_async.close_pool, close))
        while not db_async._pool_closing:
            await asyncio.sleep(0)
        queued = asyncio.create_task(run_in_db(lambda: events.append("queued")))
        await asyncio.sleep(0)

        release.set()
        await first
        await closing
        assert closed.is_set()
        await queued
        assert events == ["closed", "queued"]
    finally:
        release.set()
        if not first.done():
            await asyncio.gather(first, return_exceptions=True)


@pytest.mark.asyncio
async def test_pool_reconfiguration_finishes_before_queued_db_work_is_admitted() -> None:
    from infra import database, db_async
    from infra.db_async import run_in_db

    started = threading.Event()
    release = threading.Event()
    configured = threading.Event()

    def blocking_job() -> None:
        started.set()
        release.wait()

    first = asyncio.create_task(run_in_db(blocking_job))
    try:
        await _wait_started(started)
        closing = asyncio.create_task(
            asyncio.to_thread(database.shutdown_pool, configured.set),
        )
        while not db_async._pool_closing:
            await asyncio.sleep(0)
        queued = asyncio.create_task(run_in_db(configured.is_set))
        release.set()
        await first
        await closing
        assert await queued
    finally:
        release.set()
        if not first.done():
            await asyncio.gather(first, return_exceptions=True)


@pytest.mark.asyncio
async def test_run_in_db_cancellation_joins_exact_executor_work() -> None:
    from infra.db_async import run_in_db

    started = threading.Event()
    release = threading.Event()
    finished = threading.Event()

    def blocked_db_work() -> str:
        started.set()
        release.wait(timeout=5)
        finished.set()
        return "completed"

    task = asyncio.create_task(run_in_db(blocked_db_work))
    try:
        await _wait_started(started)

        task.cancel()
        await asyncio.sleep(0)
        assert not task.done()
        assert not finished.is_set()

        # Repeated cancellation still cannot detach the executor worker.
        task.cancel()
        await asyncio.sleep(0)
        assert not task.done()

        release.set()
        with pytest.raises(asyncio.CancelledError):
            await task
        assert finished.is_set()
    finally:
        release.set()
        if not task.done():
            await asyncio.gather(task, return_exceptions=True)


@pytest.mark.asyncio
async def test_run_in_db_cancellation_clears_worker_transaction(
    isolated_db,
) -> None:
    from infra.database import get_db
    from infra.db_async import run_in_db

    started = threading.Event()
    release = threading.Event()

    def blocked_write() -> str:
        db = get_db()
        db.execute("CREATE TABLE IF NOT EXISTS db_cancel_probe (value TEXT)")
        db.commit()
        db.execute("INSERT INTO db_cancel_probe (value) VALUES ('uncommitted')")
        started.set()
        release.wait(timeout=5)
        return "completed"

    task = asyncio.create_task(run_in_db(blocked_write))
    try:
        await _wait_started(started)

        task.cancel()
        await asyncio.sleep(0)
        assert not task.done()

        release.set()
        with pytest.raises(asyncio.CancelledError):
            await task
    finally:
        release.set()
        if not task.done():
            await asyncio.gather(task, return_exceptions=True)

    _assert_writer_available(isolated_db)
    assert _row_count(isolated_db, "db_cancel_probe") == 0


@pytest.mark.asyncio
async def test_run_in_db_rolls_back_worker_transaction_before_reraising(
    isolated_db,
) -> None:
    from infra.database import get_db
    from infra.db_async import run_in_db

    def fail_after_write() -> None:
        db = get_db()
        db.execute("CREATE TABLE IF NOT EXISTS db_job_probe (value TEXT)")
        db.commit()
        db.execute("INSERT INTO db_job_probe (value) VALUES ('uncommitted')")
        raise LookupError("worker failed")

    with pytest.raises(LookupError, match="worker failed"):
        await run_in_db(fail_after_write)

    _assert_writer_available(isolated_db)
    assert _row_count(isolated_db, "db_job_probe") == 0


@pytest.mark.asyncio
async def test_run_in_db_rejects_success_with_uncommitted_worker_write(
    isolated_db,
) -> None:
    from infra.database import TransactionOwnershipError, get_db
    from infra.db_async import run_in_db

    def return_after_write() -> str:
        db = get_db()
        db.execute("CREATE TABLE IF NOT EXISTS db_job_probe (value TEXT)")
        db.commit()
        db.execute("INSERT INTO db_job_probe (value) VALUES ('uncommitted')")
        return "success"

    with pytest.raises(TransactionOwnershipError, match="return_after_write"):
        await run_in_db(return_after_write)

    assert _row_count(isolated_db, "db_job_probe") == 0


@pytest.mark.parametrize("operation", ["setting", "model"])
def test_direct_api_write_releases_transaction_when_commit_fails(
    isolated_db,
    monkeypatch,
    operation: str,
) -> None:
    from infra.database import get_db
    from infra import settings
    from llm import catalog

    db = get_db()
    db.execute(
        "INSERT INTO models "
        "(id, harness, provider, provider_model_id, litellm_model, display_name, "
        "backend, source, enabled, available, created_at, updated_at) "
        "VALUES ('model-owner', 'native', 'omlx', 'test', 'omlx/test', 'Test', "
        "'api', 'test', 1, 1, 't', 't')"
    )
    db.commit()

    failing = _CommitFailure(db)
    target = settings if operation == "setting" else catalog
    monkeypatch.setattr(target, "get_db", lambda: failing)
    monkeypatch.setattr("infra.database.get_db", lambda: failing)

    with pytest.raises(sqlite3.OperationalError, match="forced commit failure"):
        if operation == "setting":
            settings.set_setting("features", {"enabled": True})
        else:
            catalog.update_model("model-owner", {"enabled": False})

    assert failing.closed
    _assert_writer_available(isolated_db)


@pytest.mark.asyncio
async def test_cloud_discovery_releases_transaction_when_commit_fails(
    isolated_db,
    monkeypatch,
) -> None:
    import litellm

    from infra.database import get_db
    from llm import catalog_cloud
    from unittest.mock import AsyncMock

    monkeypatch.setattr(
        "llm.external_provider.fetch_external_models",
        AsyncMock(return_value=["claude-sonnet-4"]),
    )
    db = get_db()

    failing = _CommitFailure(db)
    monkeypatch.setattr("infra.database.get_db", lambda: failing)
    monkeypatch.setattr(
        litellm,
        "model_cost",
        {
            "claude-sonnet-4": {
                "max_input_tokens": 200_000,
                "max_output_tokens": 16_000,
            }
        },
    )

    with pytest.raises(sqlite3.OperationalError, match="forced commit failure"):
        await catalog_cloud.discover_cloud_models("anthropic", "test-key")

    assert failing.closed
    _assert_writer_available(isolated_db)


@pytest.mark.asyncio
async def test_cc_tool_observer_owns_its_worker_connection(monkeypatch) -> None:
    from types import SimpleNamespace

    from core.turn_core import TurnIdentity
    from registry.registration_writer import RegistrationOrigin
    from subagents import cc_event_tool_settlement
    from subagents.cc_runtime_state import _Live

    identity = TurnIdentity("11111111-1111-4111-8111-111111111111", 1)
    live = _Live(
        session=None,
        parent_thread_id=None,
        name="cc",
        corsia_turn=identity,
    )
    live.tool_names["tool-1"] = "Read"
    live.tool_step_ids["tool-1"] = "step-1"
    live.tools_settled.clear()
    events: list[object] = []
    observed_origins: list[RegistrationOrigin] = []

    class ProbeDb:
        def close(self) -> None:
            events.append("close")

    probe_db = ProbeDb()

    async def observe(_thread_id, _content, _metadata, db, *, origin) -> None:
        events.append(("observe", db))
        observed_origins.append(origin)

    async def emit(**kwargs) -> None:
        events.append(("emit", kwargs["db"]))

    monkeypatch.setattr(
        "infra.database.get_db",
        lambda: events.append("get_db") or probe_db,
    )
    monkeypatch.setattr(cc_event_tool_settlement, "observe_tool_result", observe)
    monkeypatch.setattr(cc_event_tool_settlement, "emit_finalized_step", emit)

    settled = await cc_event_tool_settlement.settle_committed_tool_result(
        live=live,
        child_id="cc-owner",
        identity=identity,
        canonical=SimpleNamespace(
            provider_tool_call_id="tool-1",
            assistant_step_id="step-1",
            name="Read",
            content="contents",
            is_error=False,
        ),
        bcast=lambda _thread_id, _payload: None,
    )

    assert settled is True
    assert events == [
        ("observe", None),
        "get_db",
        ("emit", probe_db),
        "close",
    ]
    assert observed_origins[0].thread_id == "cc-owner"
    assert observed_origins[0].turn_id == identity.turn_id


@pytest.mark.parametrize(
    "operation",
    [
        "server_upsert",
        "server_metadata",
        "server_status",
        "server_remove",
        "models_unavailable",
        "endpoint",
    ],
)
def test_omlx_health_write_releases_transaction_when_commit_fails(
    isolated_db,
    monkeypatch,
    operation: str,
) -> None:
    from infra.database import get_db
    from llm import network_discovery, network_endpoints

    db = get_db()
    db.execute(
        "INSERT INTO network_servers "
        "(id, instance_id, instance_name, port, last_seen_at, created_at, updated_at) "
        "VALUES ('server-owner', 'instance', 'Local', 5200, 't', 't', 't')"
    )
    db.commit()

    failing = _CommitFailure(db)
    target = network_endpoints if operation == "endpoint" else network_discovery
    monkeypatch.setattr(target, "get_db", lambda: failing)
    monkeypatch.setattr("infra.database.get_db", lambda: failing)

    def write() -> None:
        if operation == "server_upsert":
            network_discovery.upsert_server(
                "omlx",
                "instance",
                "Local",
                "127.0.0.1",
                5200,
                "online",
            )
        elif operation == "server_metadata":
            network_discovery.set_server_metadata("server-owner", api_key="secret")
        elif operation == "server_status":
            network_discovery.update_server_status("server-owner", status="online")
        elif operation == "server_remove":
            network_discovery.remove_server("server-owner")
        elif operation == "models_unavailable":
            network_discovery._mark_server_models_unavailable("server-owner", "omlx")
        else:
            network_endpoints.upsert_endpoint(
                "server-owner", "127.0.0.1", ok=True, latency_ms=1
            )

    with pytest.raises(sqlite3.OperationalError, match="forced commit failure"):
        write()

    assert failing.closed
    _assert_writer_available(isolated_db)
