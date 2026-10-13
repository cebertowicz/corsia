"""FastAPI startup and shutdown lifecycle."""

from __future__ import annotations

import asyncio
import time
from contextlib import asynccontextmanager

from fastapi import FastAPI
from app_shutdown_idle_timers import fold_idle_checkpoint_shutdown, remaining_idle_timers
from app_shutdown_join import join_cleanup_owner, count_shutdown_owner
from app_shutdown_owned_tasks import shutdown_owned_tasks as _shutdown_owned_tasks
from app_shutdown_refreshes import shutdown_refresh_owners
from app_startup_runtime import (
    _start_runtime_services, _startup_harness_discovery as _startup_harness_discovery,
    configure_sqlite_fetch_switch_interval, stop_runtime_logs,
)
from app_shutdown_native import remaining_native_owners, shutdown_native_turns
import infra.app_loop as app_loop
from infra import key_store_values, state
from infra.db_async import run_in_db
from infra.database import init_db, shutdown_pool


async def _startup_cloud_discovery() -> None:
    from infra.key_store_providers import provider_keys
    from llm.catalog import discover_cloud_models
    from loguru import logger

    keys = await run_in_db(provider_keys)
    for provider, key in (
        ("openai", keys.get("openai")),
        ("anthropic", keys.get("anthropic")),
    ):
        if not key:
            continue
        try:
            result = await discover_cloud_models(provider, key)
            if result["ok"]:
                logger.info(
                    f"{provider} cloud discovery: {len(result['models'])} models"
                )
        except Exception as exc:
            logger.warning(f"{provider} cloud discovery error: {exc}")


async def _discover_network_servers(logger) -> int:
    from llm.network_discovery import _discover_and_notify
    from llm.server_observation import startup_discovery_targets

    online = await run_in_db(startup_discovery_targets)
    tasks = [asyncio.create_task(_discover_and_notify(server["id"])) for server in online]
    try:
        results = await asyncio.gather(*tasks, return_exceptions=True)
        for server, result in zip(online, results, strict=True):
            if isinstance(result, BaseException):
                logger.warning(f"network discovery startup failed for {server['id']}: {result}")
    finally:
        for task in tasks:
            if not task.done():
                task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
    return len(online)


def _reconcile_omlx_servers(servers: list[dict], logger) -> None:
    from llm.providers.omlx_supervisor import reconcile

    for server in servers:
        if server.get("provider") != "omlx":
            continue
        result = reconcile({"server_id": server["id"]})
        if result.get("adopted"):
            logger.info(
                f"oMLX supervisor: adopted pid {result['pid']} for {server['id']}"
            )
        elif result.get("cleared"):
            logger.info(
                f"oMLX supervisor: cleared stale pid {result['pid']} for {server['id']}"
            )

async def _startup_network_discovery() -> None:
    from llm.network_discovery import _sweep_all_servers, get_servers
    from loguru import logger

    try:
        servers = await run_in_db(get_servers)
        try:
            await run_in_db(_reconcile_omlx_servers, servers or [], logger)
        except Exception as exc:
            logger.warning(f"oMLX pid reconciliation failed: {exc}")
        if not servers:
            return
        await _sweep_all_servers()
        online_count = await _discover_network_servers(logger)
        logger.info(
            f"network discovery startup: probed {len(servers)} server(s), "
            f"{online_count} online, discovery complete"
        )
    except Exception as exc:
        logger.warning(f"network discovery startup failed: {exc}")


def _initialize_startup(logger) -> None:
    from app_startup_workspace import initialize_workspace
    from core.project_path_conversion import convert_legacy_project_paths
    from infra.config import LOGS_DIR, RUNTIME_DIR, UPLOADS_DIR, WORKSPACE, WORKSPACE_SELECTION
    from infra.ignored_environment import drop_ignored_environment, note_ignored_environment
    from infra.startup_prerequisites import prepare_startup_prerequisites
    from plans.public import sweep_owed_status
    from registry.durable_writes import recover_write_intents
    from registry.relocate import recover_interrupted_relocations

    note_ignored_environment()
    prepare_startup_prerequisites()
    for directory in (UPLOADS_DIR, LOGS_DIR, RUNTIME_DIR):
        directory.mkdir(parents=True, exist_ok=True)
    phase_started = time.monotonic()
    logger.info("startup phase: database initialization started")
    init_db()
    drop_ignored_environment()
    key_store_values.sweep_at_startup()
    convert_legacy_project_paths(WORKSPACE_SELECTION)
    elapsed = time.monotonic() - phase_started
    logger.info("startup phase: database initialization complete in {:.1f}s", elapsed)
    recover_write_intents()
    recover_interrupted_relocations()
    sweep_owed_status()
    initialize_workspace(WORKSPACE, logger)


@asynccontextmanager
async def lifespan(app: FastAPI):
    """Start and stop backend runtime services for the FastAPI app."""
    from loguru import logger

    configure_sqlite_fetch_switch_interval()
    _initialize_startup(logger)
    app_loop.capture(asyncio.get_running_loop())
    startup_tasks: list[asyncio.Task | None] = [None, None, None, None, None]
    body_cancel: asyncio.CancelledError | None = None
    try:
        from api.usage_rollup import schedule_refresh as schedule_usage_rollup_refresh
        from automation import restart_startup
        from doceditor.live_runtime import start_documents
        from infra.settings import get_mcp_servers
        from llm.lifecycle import autostart_local_providers
        from tools.mcp_runtime import start_mcp_runtime

        await start_mcp_runtime(await run_in_db(get_mcp_servers))

        startup_tasks[0] = asyncio.create_task(autostart_local_providers())
        startup_tasks[1] = asyncio.create_task(_startup_cloud_discovery())
        startup_tasks[2] = asyncio.create_task(_startup_network_discovery())
        startup_tasks[3] = asyncio.create_task(_startup_harness_discovery())
        recovered, wakes = await restart_startup.run_startup_reconcile(logger)
        await _start_runtime_services()
        await start_documents()
        schedule_usage_rollup_refresh()
        startup_tasks[4] = asyncio.create_task(
            restart_startup.run_startup_drive(
                startup_tasks[3], recovered, wakes, logger,
            ),
        )
        yield
    except asyncio.CancelledError as exc:
        body_cancel = exc
    finally:
        app_loop.clear()
        try:
            await _shutdown_runtime_services(startup_tasks)
        except BaseException as cleanup_failure:
            if body_cancel is not None:
                raise body_cancel from cleanup_failure
            raise
    if body_cancel is not None:
        raise body_cancel


async def _run_cleanup_owner(name: str, awaitable, logger):
    """Run one async cleanup owner without letting it skip later owners."""
    result, pending_cancel = await join_cleanup_owner(awaitable)
    failure = result if isinstance(result, BaseException) else None
    if isinstance(result, asyncio.CancelledError):
        logger.warning(f"shutdown: {name} cancelled")
    elif failure is not None:
        logger.warning(f"shutdown: {name} failed: {failure}")
    return failure, pending_cancel


async def _shutdown_checkpoints(logger):
    """Stop exact checkpoint calls before their provider transports."""
    try:
        from core.checkpoint_runtime import shutdown_checkpoint_tasks

        return await _run_cleanup_owner(
            "checkpoint cleanup", shutdown_checkpoint_tasks(), logger,
        )
    except Exception as exc:
        logger.warning(f"shutdown: checkpoint cleanup failed: {exc}")
        return exc, None


async def _shutdown_host_access(logger):
    """Gracefully detach exact lease supervisors before storage teardown."""
    try:
        from host_access.supervisor import shutdown_host_access_supervisors

        return await _run_cleanup_owner(
            "host-access supervisor cleanup",
            shutdown_host_access_supervisors(),
            logger,
        )
    except Exception as exc:
        logger.warning(f"shutdown: host-access supervisor cleanup failed: {exc}")
        return exc, None


async def _shutdown_host_processes(logger):
    """Terminate all owned host process groups after harness quiescence."""
    from host_access.processes import PROCESS_OWNER
    from host_access.supervisor import configure_host_access_terminal_callback

    try:
        return await _run_cleanup_owner(
            "host process cleanup",
            PROCESS_OWNER.shutdown(),
            logger,
        )
    except Exception as exc:
        logger.warning(f"shutdown: host process cleanup failed: {exc}")
        return exc, None
    finally:
        configure_host_access_terminal_callback(None)


def _checkpoint_task_count() -> int:
    from core.checkpoint_runtime import checkpoint_task_count

    return checkpoint_task_count()


def _host_access_owner_count() -> int:
    from host_access.supervisor import host_access_supervisor_count

    return host_access_supervisor_count()


def _host_process_owner_count() -> int:
    from host_access.processes import PROCESS_OWNER

    return PROCESS_OWNER.live_count()


async def _shutdown_harnesses(logger):
    """Attempt both harness owners and return the first shutdown failure."""
    pending_cancel = None
    failures = []
    try:
        from subagents.cc_runtime import shutdown_all as shutdown_cc

        cc_failure, cc_cancel = await _run_cleanup_owner(
            "cc subagent shutdown_all", shutdown_cc(), logger,
        )
    except Exception as exc:
        logger.warning(f"shutdown: cc subagent shutdown_all failed: {exc}")
        cc_failure, cc_cancel = exc, None
    if cc_failure is not None:
        failures.append(cc_failure)
    pending_cancel = pending_cancel or cc_cancel

    try:
        from subagents.codex_runtime import shutdown_all as shutdown_codex

        codex_failure, codex_cancel = await _run_cleanup_owner(
            "codex subagent shutdown_all", shutdown_codex(), logger,
        )
    except Exception as exc:
        logger.warning(f"shutdown: codex subagent shutdown_all failed: {exc}")
        codex_failure, codex_cancel = exc, None
    if codex_failure is not None:
        failures.append(codex_failure)
    pending_cancel = pending_cancel or codex_cancel
    return (failures[0] if failures else None), pending_cancel


def _validate_shutdown_owners(
    remaining: dict[str, object],
    failures: tuple[BaseException | None, ...],
    *,
    process_failure,
    pending_cancel,
    logger,
) -> None:
    owner_failure = next((failure for failure in failures if failure is not None), None)
    has_remaining = any(remaining.values())
    if not has_remaining and owner_failure is None:
        return
    if has_remaining:
        logger.error(f"shutdown retained live owners: {remaining}")
    if pending_cancel is not None:
        raise pending_cancel
    if has_remaining:
        raise RuntimeError(f"shutdown retained live owners: {remaining}") from owner_failure
    assert owner_failure is not None
    if owner_failure is not process_failure:
        raise owner_failure
    logger.error(
        "shutdown process cleanup reported a failure after retaining no live "
        f"owners; process exit will continue: {owner_failure}"
    )


async def _shutdown_runtime_services(
    startup_tasks: list[asyncio.Task | None],
) -> None:
    """Quiesce exact runtime owners before product and storage teardown."""
    from loguru import logger as _log_shutdown

    pending_cancel = await _shutdown_owned_tasks(startup_tasks, _log_shutdown)

    host_access_failure, host_access_cancel = await _shutdown_host_access(
        _log_shutdown,
    )
    pending_cancel = pending_cancel or host_access_cancel

    checkpoint_failure, checkpoint_cancel = await _shutdown_checkpoints(
        _log_shutdown,
    )
    pending_cancel = pending_cancel or checkpoint_cancel

    (rollup_failure, refresh_failure), refresh_cancel = await shutdown_refresh_owners(
        _log_shutdown, _run_cleanup_owner,
    )
    pending_cancel = pending_cancel or refresh_cancel

    native_cancel = await shutdown_native_turns(_log_shutdown)
    pending_cancel = pending_cancel or native_cancel

    harness_failure, harness_cancel = await _shutdown_harnesses(_log_shutdown)
    pending_cancel = pending_cancel or harness_cancel

    from tools.mcp_runtime import stop_mcp_runtime

    mcp_failure, mcp_cancel = await _run_cleanup_owner(
        "MCP sessions", stop_mcp_runtime(), _log_shutdown,
    )
    pending_cancel = pending_cancel or mcp_cancel

    pending_cancel = await fold_idle_checkpoint_shutdown(_log_shutdown, pending_cancel)

    process_failure, process_cancel = await _shutdown_host_processes(
        _log_shutdown,
    )
    pending_cancel = pending_cancel or process_cancel

    handoff_failure, handoff_cancel = await _run_cleanup_owner(
        "awaited application handoffs", app_loop.drain_awaited(), _log_shutdown,
    )
    pending_cancel = pending_cancel or handoff_cancel

    remaining_checkpoints, checkpoint_count_failure = count_shutdown_owner(
        "checkpoint", _checkpoint_task_count, _log_shutdown,
    )
    remaining_host_access, host_access_count_failure = count_shutdown_owner(
        "host-access", _host_access_owner_count, _log_shutdown,
    )
    remaining_processes, process_count_failure = count_shutdown_owner(
        "host process", _host_process_owner_count, _log_shutdown,
    )
    _validate_shutdown_owners(
        {
            "native": remaining_native_owners(),
            "awaited_handoffs": app_loop.awaited_count(),
            "checkpoints": remaining_checkpoints,
            "host_access": remaining_host_access,
            "host_processes": remaining_processes,
            "idle_checkpoint_timers": remaining_idle_timers(),
        },
        (
            checkpoint_failure, checkpoint_count_failure, host_access_count_failure,
            process_count_failure, rollup_failure, refresh_failure, harness_failure,
            process_failure, host_access_failure, mcp_failure, handoff_failure,
        ),
        process_failure=process_failure,
        pending_cancel=pending_cancel,
        logger=_log_shutdown,
    )

    from app_shutdown_storage import finish_storage_shutdown
    await finish_storage_shutdown(_log_shutdown, pending_cancel,
                                  run_cleanup_owner=_run_cleanup_owner, shutdown_pool=shutdown_pool)
