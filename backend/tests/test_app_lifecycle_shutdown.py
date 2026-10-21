"""Ownership and ordering regressions for backend lifespan teardown."""

from __future__ import annotations

import asyncio
from collections.abc import Callable
from types import SimpleNamespace

from fastapi import FastAPI
import pytest

import app_lifecycle
import app_startup_runtime
from core import checkpoint_runtime, fork_runtime
from infra import state
from infra.config import RUNTIME_DIR

# A hang guard, not a latency budget: several waits span a cold lifespan startup
# (about 0.75s unloaded), which a 1s bound fails whenever the host is busy.
_HANG_GUARD_SECONDS = 20


class _ProcessOwnerTestContract:
    """Minimal managed-process owner surface required by lifespan startup."""

    process_dir = RUNTIME_DIR / "managed-processes"

    def configure_transition_callback(self, _callback) -> None:
        return None

    def configure_long_running_capacity_callback(self, _callback) -> None:
        return None


async def _noop(*_args, **_kwargs) -> None:
    return None


async def _empty_results(*_args, **_kwargs) -> list[dict]:
    return []


def _isolate_lifespan(monkeypatch) -> None:
    """Replace external startup work while retaining real asyncio ownership."""
    import connections.harness_models as harness_models
    import connections.harness_refresh as harness_refresh
    from host_access import processes
    import llm.network_discovery as network_discovery
    from infra import frontend_build
    from infra.state import turn_buffers
    import tools.mcp_runtime as mcp_runtime
    from tools.registry import ToolRegistry

    # llm.local_sources binds get_servers at import. Importing it first keeps the
    # stub below from becoming its permanent binding when startup imports it.
    import llm.local_sources  # noqa: F401

    class _NoopProcessOwner(_ProcessOwnerTestContract):
        async def terminate_turn(
            self,
            _thread_id: str,
            _turn_id: str,
            *,
            foreground_only: bool,
            reason: str,
        ):
            del foreground_only, reason
            return ()

        async def terminate_lease(self, _lease_id: str, *, reason: str):
            del reason
            return ()

        async def shutdown(self) -> None:
            return None

        def live_count(self) -> int:
            return 0

    monkeypatch.setattr(app_lifecycle, "init_db", lambda: None)
    monkeypatch.setattr(frontend_build, "ensure_frontend_built", lambda: None)
    monkeypatch.setattr(app_startup_runtime, "rearm_idle_checkpoints", _noop)
    monkeypatch.setattr(app_startup_runtime, "nightly_job_loop", _noop)
    monkeypatch.setattr(app_startup_runtime, "_start_current_corrections_classifier", lambda: None)
    monkeypatch.setattr(app_lifecycle, "shutdown_pool", lambda: None)
    monkeypatch.setattr("infra.key_store_providers.provider_keys", lambda: {})
    monkeypatch.setattr("llm.lifecycle.get_local_providers", lambda: [])
    monkeypatch.setattr(network_discovery, "get_servers", lambda **_filters: [])
    monkeypatch.setattr(network_discovery, "health_sweep_loop", _noop)
    monkeypatch.setattr(harness_models, "_REFRESH_TASKS", {})
    monkeypatch.setattr(mcp_runtime, "_stack", None, raising=False)
    monkeypatch.setattr(mcp_runtime, "_stopped", False)
    monkeypatch.setattr(mcp_runtime, "_lock", asyncio.Lock())
    monkeypatch.setattr(mcp_runtime, "_registry", ToolRegistry())
    monkeypatch.setattr(
        "events.connectors.github_ownership.refresh_own_login", lambda: None,
    )
    monkeypatch.setattr(
        harness_refresh,
        "refresh_harness_catalogs",
        _empty_results,
    )
    monkeypatch.setattr("automation.continuation.loop.continuation_sweep_loop", _noop)
    monkeypatch.setattr(
        turn_buffers,
        "start_sweeper",
        lambda: asyncio.create_task(_noop()),
    )
    monkeypatch.setattr("subagents.cc_runtime.shutdown_all", _noop)
    monkeypatch.setattr("subagents.codex_runtime.shutdown_all", _noop)
    monkeypatch.setattr(processes, "PROCESS_OWNER", _NoopProcessOwner())
    monkeypatch.setattr("infra.product_events.flush", lambda: None)
    monkeypatch.setattr("infra.db_async.shutdown", lambda: None)

    monkeypatch.setattr(checkpoint_runtime, "reconcile_startup_checkpoints", _noop)
    monkeypatch.setattr(fork_runtime, "shutdown_forks", _noop)
    monkeypatch.setattr(fork_runtime, "fork_task_count", lambda: 0)

    monkeypatch.setattr(state, "_active_turns", {})
    monkeypatch.setattr(state, "_active_llm_tasks", {})
    monkeypatch.setattr(state, "_active_turn_tasks", {})
    for name in (
        "_delivery_recovery_task",
        "_github_ownership_login_task",
        *(attr for _label, attr in app_startup_runtime.RUNTIME_LOOP_TASKS),
    ):
        monkeypatch.setattr(state, name, None, raising=False)


async def test_shutdown_cancels_github_ownership_login_refresh(monkeypatch):
    """Shutdown joins the non-blocking GitHub ownership refresh task."""
    from unittest.mock import Mock

    for name in (
        "_github_ownership_login_task",
        *(attr for _label, attr in app_startup_runtime.RUNTIME_LOOP_TASKS),
    ):
        monkeypatch.setattr(state, name, None, raising=False)
    refresh_started = asyncio.Event()

    async def refresh() -> None:
        refresh_started.set()
        await asyncio.Event().wait()

    refresh_task = asyncio.create_task(refresh())
    await refresh_started.wait()
    monkeypatch.setattr(state, "_github_ownership_login_task", refresh_task)

    pending_cancel = await app_lifecycle._shutdown_owned_tasks([None] * 5, Mock())

    assert pending_cancel is None
    assert refresh_task.cancelled()


def _record_tail(
    monkeypatch,
    events: list[str],
    *,
    product_flush: Callable[[], None] | None = None,
) -> None:
    async def shutdown_cc() -> None:
        events.append("cc")

    async def shutdown_codex() -> None:
        events.append("codex")

    monkeypatch.setattr("subagents.cc_runtime.shutdown_all", shutdown_cc)
    monkeypatch.setattr("subagents.codex_runtime.shutdown_all", shutdown_codex)
    monkeypatch.setattr(
        "infra.product_events.flush",
        product_flush or (lambda: events.append("product")),
    )
    monkeypatch.setattr("infra.db_async.shutdown", lambda: events.append("db_async"))
    monkeypatch.setattr(
        app_lifecycle, "shutdown_pool", lambda: events.append("db_pool")
    )


async def test_frontend_prerequisite_precedes_database_and_runtime_startup(monkeypatch):
    """Every production Uvicorn entrypoint prepares assets before serving."""
    from infra import frontend_build

    _isolate_lifespan(monkeypatch)
    monkeypatch.delenv("CORSIA_TEST_MODE", raising=False)
    events: list[str] = []
    monkeypatch.setattr(
        frontend_build,
        "ensure_frontend_built",
        lambda: events.append("frontend"),
    )
    monkeypatch.setattr(app_lifecycle, "init_db", lambda: events.append("init_db"))

    async with app_lifecycle.lifespan(FastAPI()):
        assert events[:2] == ["frontend", "init_db"]


async def test_lifespan_applies_sqlite_fetch_switch_interval_before_database(monkeypatch):
    """The Uvicorn lifespan tunes GIL handoffs before DB startup can use workers."""
    import sys

    original_interval = sys.getswitchinterval()
    intervals_at_database_start: list[float] = []

    class _StopStartup(Exception):
        pass

    def stop_at_database_start(_logger) -> None:
        intervals_at_database_start.append(sys.getswitchinterval())
        raise _StopStartup

    monkeypatch.setattr(app_lifecycle, "_initialize_startup", stop_at_database_start)
    try:
        with pytest.raises(_StopStartup):
            async with app_lifecycle.lifespan(FastAPI()):
                pytest.fail("startup should stop before serving")
    finally:
        sys.setswitchinterval(original_interval)

    assert intervals_at_database_start == pytest.approx([
        app_startup_runtime.SQLITE_FETCH_SWITCH_INTERVAL_SECONDS,
    ])


async def test_predecessor_direct_uvicorn_lifespan_fails_before_database(
    monkeypatch,
    tmp_path,
):
    """The direct main:app entrypoint cannot bind after a pairing failure."""
    import subprocess
    from unittest.mock import Mock

    import main
    from infra import frontend_build

    ensure_frontend_built = frontend_build.ensure_frontend_built
    _isolate_lifespan(monkeypatch)
    monkeypatch.delenv("CORSIA_TEST_MODE", raising=False)
    init_db = Mock(side_effect=RuntimeError("database reached after frontend failure"))
    monkeypatch.setattr(frontend_build, "FRONTEND_ROOT", tmp_path)
    monkeypatch.setattr(frontend_build, "_SERVING_ROOT", tmp_path, raising=False)
    monkeypatch.setattr(
        frontend_build,
        "_CORSIA_SCRIPT",
        tmp_path / "scripts" / "corsia",
        raising=False,
    )
    monkeypatch.setattr(frontend_build, "_is_stale", lambda: True, raising=False)
    monkeypatch.setattr(
        frontend_build.subprocess,
        "run",
        lambda *args, **kwargs: subprocess.CompletedProcess(args[0], 7),
    )
    monkeypatch.setattr(
        frontend_build,
        "ensure_frontend_built",
        ensure_frontend_built,
    )
    monkeypatch.setattr(app_lifecycle, "init_db", init_db)

    with pytest.raises(RuntimeError, match="frontend release pairing failed"):
        async with app_lifecycle.lifespan(main.app):
            pytest.fail("direct Uvicorn lifespan reached request serving")

    init_db.assert_not_called()


async def test_durable_write_recovery_runs_immediately_after_schema_init(monkeypatch):
    """No request-serving startup work precedes filesystem/registry recovery."""
    from registry import durable_writes

    _isolate_lifespan(monkeypatch)
    events: list[str] = []
    monkeypatch.setattr(app_lifecycle, "init_db", lambda: events.append("init_db"))
    monkeypatch.setattr(
        durable_writes,
        "recover_write_intents",
        lambda: events.append("durable_write_recovery"),
    )

    async with app_lifecycle.lifespan(FastAPI()):
        assert events[:2] == ["init_db", "durable_write_recovery"]


async def test_checkpoint_reconciliation_runs_after_orphan_terminalization(monkeypatch):
    """Recovery sees crash-orphaned Turns only after they become terminal."""
    import automation.restart_startup as restart_startup

    _isolate_lifespan(monkeypatch)
    events: list[str] = []

    async def recover_turns(_logger):
        events.append("turn_recovery")
        return SimpleNamespace(settlements=(), batches=()), ()

    async def reconcile_checkpoints() -> None:
        events.append("checkpoint_reconciliation")

    monkeypatch.setattr(
        restart_startup,
        "reconcile_startup_continuity",
        recover_turns,
    )
    monkeypatch.setattr(
        checkpoint_runtime,
        "reconcile_startup_checkpoints",
        reconcile_checkpoints,
    )

    async with app_lifecycle.lifespan(FastAPI()):
        assert events == ["turn_recovery", "checkpoint_reconciliation"]


async def test_readiness_does_not_wait_on_a_stuck_continuity_drive(monkeypatch):
    """A recovered item that never finishes cannot hold the port bind.

    Regression for #1365: a persisted model switch whose checkpoint
    preparation never completed kept startup before the bind, so the service
    was killed unhealthy and relaunched into the same stuck item.
    """
    import automation.restart_startup as restart_startup

    _isolate_lifespan(monkeypatch)
    driving = asyncio.Event()

    async def reconcile(_logger):
        return SimpleNamespace(settlements=(), batches=()), ()

    async def never_finishes(_recovered, _wakes, _logger) -> None:
        driving.set()
        await asyncio.Event().wait()

    monkeypatch.setattr(
        restart_startup, "reconcile_startup_continuity", reconcile,
    )
    monkeypatch.setattr(
        restart_startup, "drive_startup_continuity", never_finishes,
    )

    async def boot_and_serve() -> None:
        async with app_lifecycle.lifespan(FastAPI()):
            # Reaching here is the readiness boundary: uvicorn binds the port
            # only after lifespan startup returns.
            await asyncio.wait_for(driving.wait(), 5)

    # Bounded so the regression surfaces as a failure rather than a hang.
    await asyncio.wait_for(boot_and_serve(), 20)


async def test_host_access_recovery_completes_before_serving(monkeypatch):
    """No lease can serve traffic before durable supervisors are repaired."""
    from host_access import processes
    from host_access import supervisor

    _isolate_lifespan(monkeypatch)
    events: list[str] = []
    callbacks = []

    class _ProcessOwner(_ProcessOwnerTestContract):
        async def terminate_lease(self, lease_id: str, *, reason: str):
            events.append(f"terminate:{lease_id}:{reason}")
            return ()

        async def shutdown(self) -> None:
            events.append("process_shutdown")

        def live_count(self) -> int:
            return 0

    owner = _ProcessOwner()
    monkeypatch.setattr(processes, "PROCESS_OWNER", owner)

    def configure(callback) -> None:
        callbacks.append(callback)
        events.append(
            "callback_installed" if callback is not None else "callback_reset"
        )

    async def recover() -> dict:
        assert callbacks and callbacks[-1] is not None
        await callbacks[-1](
            {
                "id": "expired-lease",
                "state": "expired",
            }
        )
        events.append("host_access_recovery")
        return {"blocked": [], "awaiting_user": [], "active": []}

    monkeypatch.setattr(
        supervisor,
        "configure_host_access_terminal_callback",
        configure,
    )
    monkeypatch.setattr(supervisor, "recover_host_access_supervisors", recover)

    async with app_lifecycle.lifespan(FastAPI()):
        assert events == [
            "callback_installed",
            "terminate:expired-lease:lease_expired",
            "host_access_recovery",
        ]

    assert events[-2:] == ["process_shutdown", "callback_reset"]


async def test_startup_continuity_waits_for_required_harness_discovery(
    monkeypatch,
):
    """Continuity waits for harness routing truth, but not provider/network discovery."""
    import connections.harness_models as harness_models
    import connections.harness_refresh as harness_refresh
    from host_access import supervisor

    _isolate_lifespan(monkeypatch)
    discovery_started = asyncio.Event()
    discovery_release = asyncio.Event()
    events: list[str] = []

    async def discover(**_kwargs):
        discovery_started.set()
        await discovery_release.wait()
        events.append("discovery_ready")
        return []

    async def recover() -> dict:
        events.append("host_access_recovery")
        return {"blocked": [], "awaiting_user": [], "active": []}

    monkeypatch.setattr(
        harness_refresh,
        "refresh_harness_catalogs",
        discover,
    )
    monkeypatch.setattr(
        supervisor,
        "recover_host_access_supervisors",
        recover,
    )

    async def run_lifespan() -> None:
        async with app_lifecycle.lifespan(FastAPI()):
            events.append("serving")
            discovery_release.set()

    task = asyncio.create_task(run_lifespan())
    try:
        await asyncio.wait_for(discovery_started.wait(), timeout=_HANG_GUARD_SECONDS)
        assert not task.done()
        assert events == []
        discovery_release.set()
        await asyncio.wait_for(task, timeout=_HANG_GUARD_SECONDS)
    finally:
        discovery_release.set()
        if not task.done():
            task.cancel()
        await asyncio.gather(task, return_exceptions=True)

    assert events[:3] == [
        "discovery_ready",
        "host_access_recovery",
        "serving",
    ]


def test_lifecycle_never_references_retired_cutover_apis() -> None:
    """Startup and shutdown own checkpoint work only through checkpoint runtime."""
    from pathlib import Path

    source = Path(app_lifecycle.__file__).read_text(encoding="utf-8")
    assert "context_cutover" not in source
    assert "portable_checkpoint" not in source


async def test_checkpoint_tasks_join_before_provider_teardown(monkeypatch):
    """Checkpoint calls stop before provider transport teardown."""
    import connections.harness_models as harness_models

    _isolate_lifespan(monkeypatch)
    events: list[str] = []

    async def shutdown_checkpoints() -> None:
        events.append("checkpoints")

    async def shutdown_providers() -> None:
        events.append("providers")

    monkeypatch.setattr(
        fork_runtime,
        "shutdown_forks",
        shutdown_checkpoints,
    )
    monkeypatch.setattr(
        harness_models,
        "shutdown_refresh_tasks",
        shutdown_providers,
    )

    async with app_lifecycle.lifespan(FastAPI()):
        events.append("serving")

    assert events[:3] == ["serving", "checkpoints", "providers"]


async def test_checkpoint_tasks_join_before_provider_and_storage_teardown(
    monkeypatch,
):
    """Checkpoint calls reach quiescence before later owners."""
    import connections.harness_models as harness_models

    _isolate_lifespan(monkeypatch)
    events: list[str] = []
    checkpoint_started = asyncio.Event()
    checkpoint_release = asyncio.Event()

    async def shutdown_checkpoints() -> None:
        events.append("checkpoint_started")
        checkpoint_started.set()
        await checkpoint_release.wait()
        events.append("checkpoint_done")

    async def shutdown_refreshes() -> None:
        events.append("refresh")

    async def run_lifespan() -> None:
        async with app_lifecycle.lifespan(FastAPI()):
            events.append("serving")

    monkeypatch.setattr(
        fork_runtime,
        "shutdown_forks",
        shutdown_checkpoints,
    )
    monkeypatch.setattr(harness_models, "shutdown_refresh_tasks", shutdown_refreshes)
    _record_tail(monkeypatch, events)

    lifespan_task = asyncio.create_task(run_lifespan())
    try:
        await asyncio.wait_for(checkpoint_started.wait(), timeout=_HANG_GUARD_SECONDS)
        assert events == ["serving", "checkpoint_started"]
        assert not lifespan_task.done()

        checkpoint_release.set()
        await asyncio.wait_for(lifespan_task, timeout=_HANG_GUARD_SECONDS)
    finally:
        checkpoint_release.set()
        if not lifespan_task.done():
            lifespan_task.cancel()
        await asyncio.gather(lifespan_task, return_exceptions=True)

    assert events == [
        "serving",
        "checkpoint_started",
        "checkpoint_done",
        "refresh",
        "cc",
        "codex",
        "product",
        "db_async",
        "db_pool",
    ]


async def test_host_access_supervisors_join_before_checkpoint_cleanup(monkeypatch):
    """Lease owners quiesce before checkpoint calls."""
    from host_access import supervisor

    _isolate_lifespan(monkeypatch)
    events: list[str] = []

    async def shutdown_host_access() -> None:
        events.append("host_access")

    async def shutdown_checkpoints() -> None:
        events.append("checkpoints")

    monkeypatch.setattr(
        supervisor,
        "shutdown_host_access_supervisors",
        shutdown_host_access,
    )
    monkeypatch.setattr(
        fork_runtime,
        "shutdown_forks",
        shutdown_checkpoints,
    )

    async with app_lifecycle.lifespan(FastAPI()):
        events.append("serving")

    assert events[:3] == ["serving", "host_access", "checkpoints"]


async def test_process_owner_shutdown_follows_all_turn_and_harness_quiescence(
    monkeypatch,
) -> None:
    """Process groups close after supervisors/turns/harnesses and before storage."""
    from host_access import processes

    _isolate_lifespan(monkeypatch)
    events: list[str] = []

    async def shutdown_supervisors(_logger):
        events.append("supervisors")
        return None, None

    async def shutdown_native(_logger):
        events.append("native")
        return None

    async def shutdown_harnesses(_logger):
        events.extend(("cc", "codex"))
        return None, None

    class _ProcessOwner(_ProcessOwnerTestContract):
        async def terminate_lease(self, _lease_id: str, *, reason: str):
            del reason
            return ()

        async def shutdown(self) -> None:
            assert events == ["supervisors", "native", "cc", "codex"]
            events.append("processes")

        def live_count(self) -> int:
            return 0

    monkeypatch.setattr(
        app_lifecycle,
        "_shutdown_host_access",
        shutdown_supervisors,
    )
    monkeypatch.setattr(app_lifecycle, "shutdown_native_turns", shutdown_native)
    monkeypatch.setattr(app_lifecycle, "_shutdown_harnesses", shutdown_harnesses)
    monkeypatch.setattr(processes, "PROCESS_OWNER", _ProcessOwner())
    monkeypatch.setattr(
        "infra.product_events.flush",
        lambda: events.append("product"),
    )
    monkeypatch.setattr(
        "infra.db_async.shutdown",
        lambda: events.append("db_async"),
    )
    monkeypatch.setattr(
        app_lifecycle,
        "shutdown_pool",
        lambda: events.append("db_pool"),
    )

    async with app_lifecycle.lifespan(FastAPI()):
        pass

    assert events == [
        "supervisors",
        "native",
        "cc",
        "codex",
        "processes",
        "product",
        "db_async",
        "db_pool",
    ]


async def test_process_cleanup_failure_without_live_residue_allows_process_exit(
    monkeypatch,
) -> None:
    """An unverifiable cleanup must not abort teardown after no owner remains."""
    _isolate_lifespan(monkeypatch)
    events: list[str] = []

    async def failed_process_cleanup(_logger):
        events.append("process_cleanup_failed")
        return RuntimeError("ownership could not be verified"), None

    monkeypatch.setattr(
        app_lifecycle,
        "_shutdown_host_processes",
        failed_process_cleanup,
    )
    monkeypatch.setattr(
        "infra.product_events.flush",
        lambda: events.append("product"),
    )
    monkeypatch.setattr(
        "infra.db_async.shutdown",
        lambda: events.append("db_async"),
    )
    monkeypatch.setattr(
        app_lifecycle,
        "shutdown_pool",
        lambda: events.append("db_pool"),
    )

    async with app_lifecycle.lifespan(FastAPI()):
        pass

    assert events == [
        "process_cleanup_failed",
        "product",
        "db_async",
        "db_pool",
    ]


async def test_process_owner_residue_blocks_product_and_storage(monkeypatch):
    """Shutdown cannot release storage while any owned process group remains."""
    from host_access import processes

    _isolate_lifespan(monkeypatch)
    events: list[str] = []

    class _ProcessOwner(_ProcessOwnerTestContract):
        async def terminate_lease(self, _lease_id: str, *, reason: str):
            del reason
            return ()

        async def shutdown(self) -> None:
            events.append("processes")

        def live_count(self) -> int:
            return 1

    monkeypatch.setattr(processes, "PROCESS_OWNER", _ProcessOwner())
    monkeypatch.setattr(
        "infra.product_events.flush",
        lambda: events.append("product"),
    )
    monkeypatch.setattr(
        "infra.db_async.shutdown",
        lambda: events.append("db_async"),
    )
    monkeypatch.setattr(
        app_lifecycle,
        "shutdown_pool",
        lambda: events.append("db_pool"),
    )

    with pytest.raises(RuntimeError, match="shutdown retained live owners"):
        async with app_lifecycle.lifespan(FastAPI()):
            pass

    assert events == ["processes"]


async def test_checkpoint_cleanup_defers_caller_cancellation(monkeypatch):
    """Cancellation cannot interrupt the exact checkpoint join snapshot."""
    _isolate_lifespan(monkeypatch)
    events: list[str] = []
    checkpoint_started = asyncio.Event()
    checkpoint_release = asyncio.Event()

    async def shutdown_checkpoints() -> None:
        events.append("checkpoint_started")
        checkpoint_started.set()
        await checkpoint_release.wait()
        events.append("checkpoint_done")

    async def run_lifespan() -> None:
        async with app_lifecycle.lifespan(FastAPI()):
            events.append("serving")

    monkeypatch.setattr(
        fork_runtime,
        "shutdown_forks",
        shutdown_checkpoints,
    )
    _record_tail(monkeypatch, events)

    lifespan_task = asyncio.create_task(run_lifespan())
    await asyncio.wait_for(checkpoint_started.wait(), timeout=_HANG_GUARD_SECONDS)
    lifespan_task.cancel("checkpoint cleanup cancelled")
    await asyncio.sleep(0)
    assert not lifespan_task.done()

    checkpoint_release.set()
    with pytest.raises(
        asyncio.CancelledError,
        match="checkpoint cleanup cancelled",
    ):
        await asyncio.wait_for(lifespan_task, timeout=_HANG_GUARD_SECONDS)

    assert events == [
        "serving",
        "checkpoint_started",
        "checkpoint_done",
        "cc",
        "codex",
        "product",
        "db_async",
        "db_pool",
    ]


async def test_surviving_checkpoint_task_blocks_product_and_storage(monkeypatch):
    """A surviving checkpoint call cannot be mistaken for a clean shutdown."""
    import connections.harness_models as harness_models

    _isolate_lifespan(monkeypatch)
    events: list[str] = []

    async def shutdown_checkpoints() -> None:
        events.append("checkpoints")

    async def shutdown_refreshes() -> None:
        events.append("refresh")

    monkeypatch.setattr(
        fork_runtime,
        "shutdown_forks",
        shutdown_checkpoints,
    )
    monkeypatch.setattr(
        fork_runtime,
        "fork_task_count",
        lambda: 1,
    )
    monkeypatch.setattr(harness_models, "shutdown_refresh_tasks", shutdown_refreshes)
    _record_tail(monkeypatch, events)

    with pytest.raises(RuntimeError, match="shutdown retained live owners"):
        async with app_lifecycle.lifespan(FastAPI()):
            pass

    assert events == ["checkpoints", "refresh", "cc", "codex"]


async def test_normal_shutdown_joins_all_owned_work_before_storage(
    monkeypatch,
):
    """Background discovery workers, loops, and Native all quiesce first."""
    import connections.harness_models as harness_models
    import connections.harness_refresh as harness_refresh
    import llm.catalog as llm_catalog
    import llm.lifecycle as llm_lifecycle
    import llm.network_discovery as network_discovery
    from infra.state import turn_buffers

    _isolate_lifespan(monkeypatch)
    events: list[str] = []
    worker_names = {
        "provider auto-start",
        "cloud discovery",
        "network discovery",
        "idle checkpoint timer",
        "nightly job",
        "network health",
        "turn-buffer sweeper",
        "plan liveness",
        "corrections classifier",
    }
    started = {name: asyncio.Event() for name in worker_names}
    stopped: set[str] = set()

    async def block(name: str):
        started[name].set()
        try:
            await asyncio.Event().wait()
        finally:
            stopped.add(name)

    monkeypatch.setattr(
        llm_lifecycle,
        "get_local_providers",
        lambda: [{"name": "local", "enabled": True, "autoStart": True}],
    )
    monkeypatch.setattr(
        llm_lifecycle,
        "check_server",
        lambda _name: block("provider auto-start"),
    )
    monkeypatch.setattr("infra.key_store_providers.provider_keys", lambda: {"openai": "key"})
    monkeypatch.setattr(
        llm_catalog,
        "discover_cloud_models",
        lambda _provider, _key: block("cloud discovery"),
    )
    monkeypatch.setattr(
        network_discovery,
        "get_servers",
        lambda: [{"id": "network", "provider": "other", "status": "offline"}],
    )
    monkeypatch.setattr(
        network_discovery,
        "_sweep_all_servers",
        lambda: block("network discovery"),
    )
    monkeypatch.setattr(
        harness_refresh,
        "refresh_harness_catalogs",
        lambda **_kwargs: asyncio.sleep(0, result=[]),
    )
    monkeypatch.setattr(
        network_discovery,
        "health_sweep_loop",
        lambda: block("network health"),
    )
    async def arm_blocking_idle_timer() -> None:
        import infra.idle_checkpoint_timers as idle_checkpoint_timers
        from infra.idle_checkpoints import IdleCheckpointDue

        due = IdleCheckpointDue("turn-idle", 1, "thread-idle", "2026-01-01T00:00:00")
        idle_checkpoint_timers.arm(
            due, 0, lambda _due: block("idle checkpoint timer"),
        )

    monkeypatch.setattr(app_startup_runtime, "rearm_idle_checkpoints", arm_blocking_idle_timer)
    monkeypatch.setattr(
        app_startup_runtime,
        "nightly_job_loop",
        lambda: block("nightly job"),
    )
    monkeypatch.setattr(
        "automation.continuation.loop.continuation_sweep_loop",
        lambda: block("plan liveness"),
    )
    monkeypatch.setattr(
        turn_buffers,
        "start_sweeper",
        lambda: asyncio.create_task(block("turn-buffer sweeper")),
    )
    monkeypatch.setattr(
        "jobs.corrections_realtime._startup_worker",
        lambda: block("corrections classifier"),
    )

    async def shutdown_refreshes() -> None:
        events.append("refresh")

    monkeypatch.setattr(harness_models, "shutdown_refresh_tasks", shutdown_refreshes)

    def flush_product() -> None:
        clean = (
            stopped == worker_names
            and not state._active_turns
            and not state._active_turn_tasks
        )
        events.append("product_clean" if clean else "product_live")

    _record_tail(monkeypatch, events, product_flush=flush_product)

    native_started = asyncio.Event()
    native_cancel = asyncio.Event()
    native_task: asyncio.Task | None = None

    async def native_turn() -> None:
        native_started.set()
        try:
            await native_cancel.wait()
        finally:
            stopped.add("native")
            if state._active_turns.get("native") is native_cancel:
                state._active_turns.pop("native", None)
            if state._active_turn_tasks.get("native") is asyncio.current_task():
                state._active_turn_tasks.pop("native", None)

    worker_names.add("native")
    try:
        async with app_lifecycle.lifespan(FastAPI()):
            native_task = asyncio.create_task(native_turn())
            state._active_turns["native"] = native_cancel
            state._active_turn_tasks["native"] = native_task
            await asyncio.gather(
                native_started.wait(),
                *(event.wait() for event in started.values()),
            )
    finally:
        native_cancel.set()
        if native_task is not None:
            await asyncio.gather(native_task, return_exceptions=True)

    assert events == [
        "refresh",
        "cc",
        "codex",
        "product_clean",
        "db_async",
        "db_pool",
    ]


async def test_body_exception_still_runs_complete_teardown(monkeypatch):
    import connections.harness_models as harness_models

    _isolate_lifespan(monkeypatch)
    events: list[str] = []
    worker_started = asyncio.Event()

    async def worker() -> None:
        worker_started.set()
        try:
            await asyncio.Event().wait()
        finally:
            events.append("worker_stopped")

    async def shutdown_refreshes() -> None:
        events.append("refresh")

    monkeypatch.setattr(app_startup_runtime, "nightly_job_loop", worker)
    monkeypatch.setattr(harness_models, "shutdown_refresh_tasks", shutdown_refreshes)
    _record_tail(monkeypatch, events)

    with pytest.raises(RuntimeError, match="body failed"):
        async with app_lifecycle.lifespan(FastAPI()):
            await worker_started.wait()
            raise RuntimeError("body failed")

    assert events == [
        "worker_stopped",
        "refresh",
        "cc",
        "codex",
        "product",
        "db_async",
        "db_pool",
    ]


async def test_startup_setup_failure_joins_created_work_before_storage(monkeypatch):
    """A failure before ``yield`` still closes every owner already created."""
    import connections.harness_models as harness_models
    from infra.state import turn_buffers

    _isolate_lifespan(monkeypatch)
    events: list[str] = []
    created_tasks: list[asyncio.Task] = []
    create_task = asyncio.create_task

    def track_task(coro, **kwargs) -> asyncio.Task:
        task = create_task(coro, **kwargs)
        created_tasks.append(task)
        return task

    def fail_sweeper_start() -> asyncio.Task:
        raise RuntimeError("turn-buffer sweeper setup failed")

    async def shutdown_refreshes() -> None:
        events.append("refresh")

    def flush_product() -> None:
        state_name = "clean" if all(task.done() for task in created_tasks) else "live"
        events.append(f"product_{state_name}")

    monkeypatch.setattr(app_lifecycle.asyncio, "create_task", track_task)
    monkeypatch.setattr(turn_buffers, "start_sweeper", fail_sweeper_start)
    monkeypatch.setattr(harness_models, "shutdown_refresh_tasks", shutdown_refreshes)
    _record_tail(monkeypatch, events, product_flush=flush_product)

    try:
        with pytest.raises(
            RuntimeError,
            match="turn-buffer sweeper setup failed",
        ):
            async with app_lifecycle.lifespan(FastAPI()):
                pytest.fail("lifespan body must not start")

        assert created_tasks
        assert all(task.done() for task in created_tasks)
        assert events == [
            "refresh",
            "cc",
            "codex",
            "product_clean",
            "db_async",
            "db_pool",
        ]
    finally:
        for task in created_tasks:
            if not task.done():
                task.cancel()
        await asyncio.gather(*created_tasks, return_exceptions=True)


async def test_body_cancellation_still_runs_complete_teardown(monkeypatch):
    import connections.harness_models as harness_models

    _isolate_lifespan(monkeypatch)
    events: list[str] = []
    body_started = asyncio.Event()
    worker_started = asyncio.Event()

    async def worker() -> None:
        worker_started.set()
        try:
            await asyncio.Event().wait()
        finally:
            events.append("worker_stopped")

    async def shutdown_refreshes() -> None:
        events.append("refresh")

    async def run_lifespan() -> None:
        async with app_lifecycle.lifespan(FastAPI()):
            body_started.set()
            await asyncio.Event().wait()

    monkeypatch.setattr(app_startup_runtime, "nightly_job_loop", worker)
    monkeypatch.setattr(harness_models, "shutdown_refresh_tasks", shutdown_refreshes)
    _record_tail(monkeypatch, events)

    lifespan_task = asyncio.create_task(run_lifespan())
    await asyncio.gather(body_started.wait(), worker_started.wait())
    lifespan_task.cancel("body cancelled")
    with pytest.raises(asyncio.CancelledError, match="body cancelled"):
        await lifespan_task

    assert events == [
        "worker_stopped",
        "refresh",
        "cc",
        "codex",
        "product",
        "db_async",
        "db_pool",
    ]


async def test_body_cancellation_wins_over_harness_owner_failure(monkeypatch):
    """The caller's body cancellation survives a fail-closed owner result."""
    import connections.harness_models as harness_models

    _isolate_lifespan(monkeypatch)
    events: list[str] = []
    body_started = asyncio.Event()

    async def shutdown_refreshes() -> None:
        events.append("refresh")

    async def shutdown_cc() -> None:
        events.append("cc")
        raise RuntimeError("cc live owners remain")

    async def shutdown_codex() -> None:
        events.append("codex")

    async def run_lifespan() -> None:
        async with app_lifecycle.lifespan(FastAPI()):
            body_started.set()
            await asyncio.Event().wait()

    monkeypatch.setattr(harness_models, "shutdown_refresh_tasks", shutdown_refreshes)
    monkeypatch.setattr("subagents.cc_runtime.shutdown_all", shutdown_cc)
    monkeypatch.setattr("subagents.codex_runtime.shutdown_all", shutdown_codex)
    monkeypatch.setattr("infra.product_events.flush", lambda: events.append("product"))
    monkeypatch.setattr("infra.db_async.shutdown", lambda: events.append("db_async"))
    monkeypatch.setattr(
        app_lifecycle, "shutdown_pool", lambda: events.append("db_pool")
    )

    lifespan_task = asyncio.create_task(run_lifespan())
    await body_started.wait()
    lifespan_task.cancel("body cancelled")
    with pytest.raises(asyncio.CancelledError, match="body cancelled") as caught:
        await lifespan_task

    assert isinstance(caught.value.__cause__, RuntimeError)
    assert str(caught.value.__cause__) == "cc live owners remain"
    assert events == ["refresh", "cc", "codex"]


async def test_cancellation_during_cleanup_is_deferred_until_all_owners_finish(
    monkeypatch,
):
    import connections.harness_models as harness_models

    _isolate_lifespan(monkeypatch)
    events: list[str] = []
    cc_started = asyncio.Event()
    cc_release = asyncio.Event()

    async def shutdown_refreshes() -> None:
        events.append("refresh")

    async def shutdown_cc() -> None:
        events.append("cc_started")
        cc_started.set()
        await cc_release.wait()
        events.append("cc_done")

    async def run_lifespan() -> None:
        async with app_lifecycle.lifespan(FastAPI()):
            events.append("serving")

    monkeypatch.setattr(harness_models, "shutdown_refresh_tasks", shutdown_refreshes)
    _record_tail(monkeypatch, events)
    monkeypatch.setattr("subagents.cc_runtime.shutdown_all", shutdown_cc)

    lifespan_task = asyncio.create_task(run_lifespan())
    await cc_started.wait()
    lifespan_task.cancel("cleanup cancelled")
    await asyncio.sleep(0)
    assert not lifespan_task.done()

    cc_release.set()
    with pytest.raises(asyncio.CancelledError, match="cleanup cancelled"):
        await lifespan_task

    assert events == [
        "serving",
        "refresh",
        "cc_started",
        "cc_done",
        "codex",
        "product",
        "db_async",
        "db_pool",
    ]


@pytest.mark.parametrize("owner", ["checkpoint", "refresh", "cc", "codex"])
async def test_runtime_owner_failure_blocks_product_and_storage(monkeypatch, owner):
    """A failed runtime join cannot be mistaken for a safe storage boundary."""
    import connections.harness_models as harness_models

    _isolate_lifespan(monkeypatch)
    events: list[str] = []
    failure_started = asyncio.Event()
    failure_release = asyncio.Event()

    async def shutdown_failure() -> None:
        events.append(f"{owner}_started")
        failure_started.set()
        await failure_release.wait()
        raise RuntimeError(f"{owner} live owners remain")

    async def shutdown_cc() -> None:
        events.append("cc")

    async def shutdown_codex() -> None:
        events.append("codex")

    async def shutdown_refreshes() -> None:
        events.append("refresh")

    async def run_lifespan() -> None:
        async with app_lifecycle.lifespan(FastAPI()):
            events.append("serving")

    monkeypatch.setattr(
        fork_runtime,
        "shutdown_forks",
        shutdown_failure if owner == "checkpoint" else _noop,
    )
    monkeypatch.setattr(
        harness_models,
        "shutdown_refresh_tasks",
        shutdown_failure if owner == "refresh" else shutdown_refreshes,
    )
    monkeypatch.setattr(
        "subagents.cc_runtime.shutdown_all",
        shutdown_failure if owner == "cc" else shutdown_cc,
    )
    monkeypatch.setattr(
        "subagents.codex_runtime.shutdown_all",
        shutdown_failure if owner == "codex" else shutdown_codex,
    )
    monkeypatch.setattr("infra.product_events.flush", lambda: events.append("product"))
    monkeypatch.setattr("infra.db_async.shutdown", lambda: events.append("db_async"))
    monkeypatch.setattr(
        app_lifecycle, "shutdown_pool", lambda: events.append("db_pool")
    )

    lifespan_task = asyncio.create_task(run_lifespan())
    await failure_started.wait()
    before_failure = {
        "checkpoint": ["serving", "checkpoint_started"],
        "refresh": ["serving", "refresh_started"],
        "cc": ["serving", "refresh", "cc_started"],
        "codex": ["serving", "refresh", "cc", "codex_started"],
    }[owner]
    assert events == before_failure
    assert not lifespan_task.done()

    failure_release.set()
    with pytest.raises(RuntimeError, match=f"{owner} live owners remain"):
        await lifespan_task

    expected = {
        "checkpoint": ["serving", "checkpoint_started", "refresh", "cc", "codex"],
        "refresh": ["serving", "refresh_started", "cc", "codex"],
        "cc": ["serving", "refresh", "cc_started", "codex"],
        "codex": before_failure,
    }[owner]
    assert events == expected


async def test_surviving_native_owner_blocks_product_and_storage(monkeypatch):
    """Native teardown fails closed when no exact join handle proves it done."""
    _isolate_lifespan(monkeypatch)
    events: list[str] = []
    thread_id = "native-survivor"
    state._active_turns[thread_id] = asyncio.Event()
    monkeypatch.setattr("infra.product_events.flush", lambda: events.append("product"))
    monkeypatch.setattr("infra.db_async.shutdown", lambda: events.append("db_async"))
    monkeypatch.setattr(
        app_lifecycle, "shutdown_pool", lambda: events.append("db_pool")
    )

    try:
        with pytest.raises(RuntimeError, match="shutdown retained live owners"):
            async with app_lifecycle.lifespan(FastAPI()):
                pass
        assert events == []
    finally:
        state._active_turns.pop(thread_id, None)


@pytest.mark.parametrize(
    "replaced_owner",
    ["cancel_handle", "turn_task", "llm_task"],
)
async def test_native_snapshot_does_not_signal_a_pre_call_successor(
    monkeypatch,
    replaced_owner,
):
    """A replaced Native generation remains untouched and blocks storage."""
    from core.turn_core import TurnIdentity
    from core.native_turn_control import TurnCancelEvent

    _isolate_lifespan(monkeypatch)
    events: list[str] = []
    first_cancel = TurnCancelEvent()
    first_cancel.bind_turn_identity(TurnIdentity("turn-first", 1))
    first_cleanup_started = asyncio.Event()
    first_cleanup_release = asyncio.Event()
    old_cancel = TurnCancelEvent()
    old_cancel.bind_turn_identity(TurnIdentity("turn-replaced-old", 2))
    successor_cancel = TurnCancelEvent()
    successor_cancel.bind_turn_identity(TurnIdentity("turn-replaced-new", 3))

    async def first_turn() -> None:
        await first_cancel.wait()
        first_cleanup_started.set()
        await first_cleanup_release.wait()
        if not first_cancel.completion.done():
            first_cancel.completion.set_result(True)

    async def wait_for_cancel(cancel: asyncio.Event) -> None:
        await cancel.wait()

    first_task = asyncio.create_task(first_turn())
    old_task = asyncio.create_task(wait_for_cancel(old_cancel))
    old_llm_task = asyncio.create_task(asyncio.Event().wait())
    successor_task = asyncio.create_task(
        wait_for_cancel(
            successor_cancel if replaced_owner == "cancel_handle" else old_cancel,
        )
    )
    successor_llm_task = asyncio.create_task(asyncio.Event().wait())
    state._active_turns["first"] = first_cancel
    state._active_turn_tasks["first"] = first_task
    state._active_turns["replaced"] = old_cancel
    state._active_turn_tasks["replaced"] = old_task
    state._active_llm_tasks["replaced"] = old_llm_task
    monkeypatch.setattr("infra.product_events.flush", lambda: events.append("product"))
    monkeypatch.setattr("infra.db_async.shutdown", lambda: events.append("db_async"))
    monkeypatch.setattr(
        app_lifecycle, "shutdown_pool", lambda: events.append("db_pool")
    )

    async def run_lifespan() -> None:
        async with app_lifecycle.lifespan(FastAPI()):
            pass

    lifespan_task = asyncio.create_task(run_lifespan())
    try:
        await asyncio.wait_for(first_cleanup_started.wait(), timeout=_HANG_GUARD_SECONDS)
        if replaced_owner == "cancel_handle":
            state._active_turns["replaced"] = successor_cancel
            state._active_turn_tasks["replaced"] = successor_task
        elif replaced_owner == "turn_task":
            state._active_turn_tasks["replaced"] = successor_task
        else:
            state._active_llm_tasks["replaced"] = successor_llm_task
        first_cleanup_release.set()

        with pytest.raises(RuntimeError, match="shutdown retained live owners"):
            await asyncio.wait_for(lifespan_task, timeout=_HANG_GUARD_SECONDS)

        if replaced_owner == "cancel_handle":
            assert not successor_cancel.is_set()
            assert not successor_task.done()
            assert state._active_turns["replaced"] is successor_cancel
            assert state._active_turn_tasks["replaced"] is successor_task
        elif replaced_owner == "turn_task":
            assert not old_cancel.is_set()
            assert not successor_task.done()
            assert state._active_turns["replaced"] is old_cancel
            assert state._active_turn_tasks["replaced"] is successor_task
        else:
            assert not old_cancel.is_set()
            assert not successor_llm_task.done()
            assert state._active_turns["replaced"] is old_cancel
            assert state._active_llm_tasks["replaced"] is successor_llm_task
        assert events == []
    finally:
        first_cleanup_release.set()
        tasks = (
            first_task,
            old_task,
            old_llm_task,
            successor_task,
            successor_llm_task,
            lifespan_task,
        )
        for task in tasks:
            if not task.done():
                task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        for thread_id in ("first", "replaced"):
            state._active_turns.pop(thread_id, None)
            state._active_turn_tasks.pop(thread_id, None)
            state._active_llm_tasks.pop(thread_id, None)


@pytest.mark.parametrize("shutdown_outcome", ["caller_cancel", "successor"])
async def test_refresh_shutdown_preserves_successor_and_reports_incomplete_join(
    monkeypatch,
    shutdown_outcome,
):
    import connections.harness_models as harness_models

    monkeypatch.setattr(harness_models, "_REFRESH_TASKS", {})
    cleanup_started = asyncio.Event()
    cleanup_release = asyncio.Event()

    async def refresh() -> dict:
        try:
            await asyncio.Event().wait()
        finally:
            cleanup_started.set()
            while not cleanup_release.is_set():
                try:
                    await cleanup_release.wait()
                except asyncio.CancelledError:
                    continue
        return {"ok": True}

    old_task = asyncio.create_task(refresh())
    harness_models._REFRESH_TASKS["codex"] = old_task
    shutdown_task = asyncio.create_task(harness_models.shutdown_refresh_tasks())
    successor = asyncio.create_task(asyncio.Event().wait())
    try:
        await asyncio.wait_for(cleanup_started.wait(), timeout=_HANG_GUARD_SECONDS)
        harness_models._REFRESH_TASKS["codex"] = successor
        if shutdown_outcome == "caller_cancel":
            shutdown_task.cancel("refresh shutdown cancelled")
            await asyncio.sleep(0)
            assert not shutdown_task.done()

        cleanup_release.set()
        expected = (
            (asyncio.CancelledError, "refresh shutdown cancelled")
            if shutdown_outcome == "caller_cancel"
            else (RuntimeError, "harness refresh tasks remain")
        )
        with pytest.raises(expected[0], match=expected[1]):
            await asyncio.wait_for(shutdown_task, timeout=_HANG_GUARD_SECONDS)

        assert old_task.done()
        assert harness_models._REFRESH_TASKS["codex"] is successor
    finally:
        cleanup_release.set()
        for task in (old_task, shutdown_task, successor):
            if not task.done():
                task.cancel()
        await asyncio.gather(old_task, shutdown_task, successor, return_exceptions=True)
        harness_models._REFRESH_TASKS.clear()


async def test_network_startup_owns_and_joins_stubborn_discovery_child(
    monkeypatch,
):
    import llm.network_discovery as network_discovery

    _isolate_lifespan(monkeypatch)
    events: list[str] = []
    child_started = asyncio.Event()
    cleanup_started = asyncio.Event()
    cleanup_release = asyncio.Event()
    child_stopped = asyncio.Event()

    monkeypatch.setattr(
        network_discovery,
        "get_servers",
        lambda: [{"id": "network", "provider": "other", "status": "online"}],
    )
    monkeypatch.setattr(network_discovery, "_sweep_all_servers", _noop)

    async def discover(_server_id: str) -> None:
        child_started.set()
        try:
            await asyncio.Event().wait()
        except asyncio.CancelledError:
            cleanup_started.set()
            while not cleanup_release.is_set():
                try:
                    await cleanup_release.wait()
                except asyncio.CancelledError:
                    continue
            child_stopped.set()

    def flush_product() -> None:
        events.append("product_clean" if child_stopped.is_set() else "product_live")

    async def run_lifespan() -> None:
        async with app_lifecycle.lifespan(FastAPI()):
            await child_started.wait()

    monkeypatch.setattr(network_discovery, "_discover_and_notify", discover)
    _record_tail(monkeypatch, events, product_flush=flush_product)

    lifespan_task = asyncio.create_task(run_lifespan())
    try:
        await asyncio.wait_for(cleanup_started.wait(), timeout=_HANG_GUARD_SECONDS)
        await asyncio.sleep(0)
        assert not lifespan_task.done()
        assert events == []

        cleanup_release.set()
        await asyncio.wait_for(lifespan_task, timeout=_HANG_GUARD_SECONDS)
    finally:
        cleanup_release.set()
        if not lifespan_task.done():
            lifespan_task.cancel()
        await asyncio.gather(lifespan_task, return_exceptions=True)

    assert events == ["cc", "codex", "product_clean", "db_async", "db_pool"]


async def test_native_residue_is_joined_and_only_exact_handles_are_removed(
    monkeypatch,
):
    from core import native_turn_control

    _isolate_lifespan(monkeypatch)
    task_only_started = asyncio.Event()
    task_only_stopped = asyncio.Event()
    old_cancel = asyncio.Event()
    replaced_cancel = asyncio.Event()
    successor_cancel = asyncio.Event()

    async def task_only_turn() -> None:
        task_only_started.set()
        try:
            await asyncio.Event().wait()
        finally:
            task_only_stopped.set()

    task_only = asyncio.create_task(task_only_turn())
    completed = asyncio.create_task(asyncio.sleep(0))
    replaced = asyncio.create_task(asyncio.sleep(0))
    successor = asyncio.create_task(asyncio.sleep(0))
    await asyncio.gather(completed, replaced, successor)

    async def cancel_native(thread_id: str) -> bool:
        if thread_id == "task-only":
            return False
        if thread_id == "replaced":
            state._active_turns[thread_id] = successor_cancel
            state._active_turn_tasks[thread_id] = successor
        return True

    monkeypatch.setattr(
        native_turn_control,
        "cancel_and_join_native_turn",
        cancel_native,
    )
    state._active_turn_tasks["task-only"] = task_only
    state._active_turns["completed"] = old_cancel
    state._active_turn_tasks["completed"] = completed
    state._active_turns["replaced"] = replaced_cancel
    state._active_turn_tasks["replaced"] = replaced
    try:
        with pytest.raises(RuntimeError, match="shutdown retained live owners"):
            async with app_lifecycle.lifespan(FastAPI()):
                await task_only_started.wait()

        assert task_only_stopped.is_set()
        assert "task-only" not in state._active_turn_tasks
        assert "completed" not in state._active_turns
        assert "completed" not in state._active_turn_tasks
        assert state._active_turns["replaced"] is successor_cancel
        assert state._active_turn_tasks["replaced"] is successor
    finally:
        if not task_only.done():
            task_only.cancel()
        await asyncio.gather(task_only, return_exceptions=True)
        for thread_id in ("task-only", "completed", "replaced"):
            state._active_turns.pop(thread_id, None)
            state._active_turn_tasks.pop(thread_id, None)


@pytest.mark.parametrize(
    "failed_owner",
    [
        "owned_task",
        "native",
        "product",
        "db_async",
        "db_pool",
    ],
)
async def test_one_owner_failure_does_not_skip_other_teardown(
    monkeypatch,
    failed_owner,
):
    import connections.harness_models as harness_models
    from core import native_turn_control

    _isolate_lifespan(monkeypatch)
    events: list[str] = []
    worker_started = asyncio.Event()
    worker_stopped = asyncio.Event()

    async def worker() -> None:
        worker_started.set()
        try:
            await asyncio.Event().wait()
        finally:
            worker_stopped.set()
            if failed_owner == "owned_task":
                raise RuntimeError("owned task failed")

    async def shutdown_refreshes() -> None:
        events.append("refresh")

    async def cancel_native(_thread_id: str) -> bool:
        events.append("native")
        if failed_owner == "native":
            raise RuntimeError("native failed")
        return True

    async def shutdown_cc() -> None:
        events.append("cc")

    async def shutdown_codex() -> None:
        events.append("codex")

    def flush_product() -> None:
        events.append("product" if worker_stopped.is_set() else "product_early")
        if failed_owner == "product":
            raise RuntimeError("product failed")

    def shutdown_db_async() -> None:
        events.append("db_async")
        if failed_owner == "db_async":
            raise RuntimeError("db async failed")

    def shutdown_db_pool() -> None:
        events.append("db_pool")
        if failed_owner == "db_pool":
            raise RuntimeError("db pool failed")

    monkeypatch.setattr(app_startup_runtime, "nightly_job_loop", worker)
    monkeypatch.setattr(harness_models, "shutdown_refresh_tasks", shutdown_refreshes)
    monkeypatch.setattr(
        native_turn_control,
        "cancel_and_join_native_turn",
        cancel_native,
    )
    monkeypatch.setattr("subagents.cc_runtime.shutdown_all", shutdown_cc)
    monkeypatch.setattr("subagents.codex_runtime.shutdown_all", shutdown_codex)
    monkeypatch.setattr("infra.product_events.flush", flush_product)
    monkeypatch.setattr("infra.db_async.shutdown", shutdown_db_async)
    monkeypatch.setattr(app_lifecycle, "shutdown_pool", shutdown_db_pool)

    async def exercise() -> None:
        async with app_lifecycle.lifespan(FastAPI()):
            await worker_started.wait()
            completed = asyncio.create_task(asyncio.sleep(0))
            await completed
            state._active_turns["native"] = asyncio.Event()
            state._active_turn_tasks["native"] = completed

    if failed_owner in {"db_async", "db_pool"}:
        message = "db async failed" if failed_owner == "db_async" else "db pool failed"
        with pytest.raises(RuntimeError, match=message):
            await exercise()
    else:
        await exercise()

    assert events == [
        "refresh",
        "native",
        "cc",
        "codex",
        "product",
        "db_async",
        "db_pool",
    ]


def _isolate_rollup_lifespan(monkeypatch) -> None:
    """Keep rollup lifecycle checks free of unrelated startup-continuity tasks."""
    from automation import restart_startup

    _isolate_lifespan(monkeypatch)

    async def startup_reconcile(_logger):
        return SimpleNamespace(batches=[], settlements=[]), ()

    monkeypatch.setattr(restart_startup, "run_startup_reconcile", startup_reconcile)
    monkeypatch.setattr(restart_startup, "run_startup_drive", _noop)


async def test_startup_rollup_refresh_does_not_delay_lifespan_yield(monkeypatch):
    """The startup warm-up starts but never becomes a readiness dependency."""
    from api import usage_rollup

    _isolate_rollup_lifespan(monkeypatch)
    refresh_started = asyncio.Event()
    release_refresh = asyncio.Event()
    yielded = asyncio.Event()

    async def refresh(_read, _write) -> None:
        refresh_started.set()
        await release_refresh.wait()

    async def run_lifespan() -> None:
        async with app_lifecycle.lifespan(FastAPI()):
            yielded.set()
            await release_refresh.wait()

    monkeypatch.setattr(usage_rollup, "_refresh", refresh)
    lifespan_task = asyncio.create_task(run_lifespan())
    try:
        await asyncio.wait_for(refresh_started.wait(), timeout=_HANG_GUARD_SECONDS)
        await asyncio.wait_for(yielded.wait(), timeout=_HANG_GUARD_SECONDS)
    finally:
        release_refresh.set()
        await asyncio.gather(lifespan_task, return_exceptions=True)


async def test_startup_and_measurement_dependencies_share_one_rollup_refresh(monkeypatch):
    """The boot trigger joins concurrent Measurement request triggers to one task."""
    from api import usage_rollup

    _isolate_rollup_lifespan(monkeypatch)
    refresh_started = asyncio.Event()
    release_refresh = asyncio.Event()
    calls = 0

    async def refresh(_read, _write) -> None:
        nonlocal calls
        calls += 1
        refresh_started.set()
        await release_refresh.wait()

    monkeypatch.setattr(usage_rollup, "_refresh", refresh)
    try:
        async with app_lifecycle.lifespan(FastAPI()):
            await asyncio.wait_for(refresh_started.wait(), timeout=_HANG_GUARD_SECONDS)
            startup_task = usage_rollup._refreshing[asyncio.get_running_loop()]
            await asyncio.gather(*(usage_rollup.refresh_on_request() for _ in range(5)))
            assert calls == 1
            assert usage_rollup._refreshing[asyncio.get_running_loop()] is startup_task
    finally:
        release_refresh.set()
        task = usage_rollup._refreshing.get(asyncio.get_running_loop())
        if task is not None:
            await asyncio.gather(task, return_exceptions=True)


async def test_shutdown_joins_rollup_refresh_before_database_pool_closes(monkeypatch):
    """A cancelled warm-up cannot write after the database pool is closed."""
    from api import usage_rollup

    _isolate_rollup_lifespan(monkeypatch)
    refresh_started = asyncio.Event()
    events: list[str] = []

    async def refresh(_read, _write) -> None:
        refresh_started.set()
        try:
            await asyncio.Event().wait()
        finally:
            events.append("refresh_finished" if "pool_closed" not in events else "write_after_pool")

    monkeypatch.setattr(usage_rollup, "_refresh", refresh)
    monkeypatch.setattr(app_lifecycle, "shutdown_pool", lambda: events.append("pool_closed"))

    async def exercise() -> None:
        async with app_lifecycle.lifespan(FastAPI()):
            await refresh_started.wait()

    await asyncio.wait_for(exercise(), timeout=_HANG_GUARD_SECONDS)
    assert events == ["refresh_finished", "pool_closed"]
