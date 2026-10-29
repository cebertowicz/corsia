"""FastAPI startup and shutdown lifecycle."""

from __future__ import annotations

import asyncio
import app_startup_init
from contextlib import asynccontextmanager

from fastapi import FastAPI
from app_shutdown_idle_timers import fold_idle_checkpoint_shutdown, remaining_idle_timers
from app_shutdown_join import join_cleanup_owner, count_shutdown_owner
from app_shutdown_owned_tasks import shutdown_owned_tasks as _shutdown_owned_tasks
from app_shutdown_refreshes import shutdown_refresh_owners
from app_startup_runtime import (
    _start_runtime_services, _startup_harness_discovery as _startup_harness_discovery,
    _startup_cloud_discovery as _startup_cloud_discovery,
    configure_sqlite_fetch_switch_interval, resume_local_setup, stop_runtime_logs,
)
from app_shutdown_native import remaining_native_owners, shutdown_native_turns
import infra.app_loop as app_loop
import infra.startup_progress as startup_progress
from infra import state
from infra.db_async import run_in_db
from infra.database import shutdown_pool
from llm.providers.omlx_supervisor import stop_watching as stop_watching_omlx


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


def _reconcile_omlx_servers(servers: list[dict], logger) -> dict[str, int]:
    """Adopt or clear each oMLX pid record; return the adopted server ids with their pids."""
    from llm.providers.omlx_supervisor import reconcile

    adopted: dict[str, int] = {}
    for server in servers:
        if server.get("provider") != "omlx":
            continue
        result = reconcile({"server_id": server["id"]})
        if result.get("adopted"):
            adopted[server["id"]] = result["pid"]
            logger.info(
                f"oMLX supervisor: adopted pid {result['pid']} for {server['id']}"
            )
        elif result.get("cleared"):
            logger.info(
                f"oMLX supervisor: cleared stale pid {result['pid']} for {server['id']}"
            )
    return adopted

async def _startup_network_discovery() -> None:
    from llm.network_discovery import _sweep_all_servers, get_servers
    from loguru import logger

    try:
        servers = await run_in_db(get_servers)
        try:
            from llm.providers.omlx_supervisor import watch

            adopted = await run_in_db(_reconcile_omlx_servers, servers or [], logger)
            for server_id, pid in adopted.items():
                watch(server_id, pid)
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


@asynccontextmanager
async def lifespan(app: FastAPI):
    """Start and stop backend runtime services for the FastAPI app."""
    from loguru import logger

    configure_sqlite_fetch_switch_interval()
    with startup_progress.publishing():
        app_startup_init.initialize_startup(logger)
    app_loop.capture(asyncio.get_running_loop())
    startup_tasks: list[asyncio.Task | None] = [None, None, None, None, None]
    body_cancel: asyncio.CancelledError | None = None
    try:
        from api.usage_rollup import schedule_refresh as schedule_usage_rollup_refresh
        from automation import restart_startup
        from doceditor.live_runtime import start_documents
        from connections.account_leftovers import remove_startup_leftovers
        from llm.lifecycle import autostart_local_providers
        from tools.agent_shell_tools import start_prefetch
        from tools.mcp_runtime import start_connected_app_runtime
        from host_access.browser_orphan import close_dead_sessions
        from host_access.processes import PROCESS_OWNER

        await asyncio.to_thread(close_dead_sessions, PROCESS_OWNER.browser_sessions().socket_dir)
        await remove_startup_leftovers()
        start_prefetch()
        await start_connected_app_runtime()

        startup_tasks[0] = asyncio.create_task(autostart_local_providers())
        startup_tasks[1] = asyncio.create_task(_startup_cloud_discovery())
        startup_tasks[2] = asyncio.create_task(_startup_network_discovery())
        startup_tasks[3] = asyncio.create_task(_startup_harness_discovery())
        recovered, wakes, rolled_back_operation = await restart_startup.run_startup_reconcile(logger)
        await _start_runtime_services()
        from core.devices_runtime import runtime as devices_runtime
        from infra.settings import get_devices_enabled, get_devices_outside_address
        enabled, outside_address = await asyncio.gather(run_in_db(get_devices_enabled), run_in_db(get_devices_outside_address))
        await devices_runtime.restore(app, enabled, outside_address)
        await start_documents()
        await resume_local_setup()
        schedule_usage_rollup_refresh()
        from infra.usage_summary import record_current_install_week

        await run_in_db(record_current_install_week)
        startup_tasks[4] = asyncio.create_task(
            restart_startup.run_startup_drive(
                startup_tasks[3], recovered, wakes, rolled_back_operation, logger,
            ),
        )
        startup_progress.finish()
        yield
    except asyncio.CancelledError as exc:
        body_cancel = exc
    finally:
        startup_progress.fail("startup stopped before it finished")
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


async def _shutdown_forks(logger):
    """Stop disposable forks before provider transports and checkpoint wakes."""
    try:
        from app_shutdown_runtime import shutdown_fork_owners

        return await _run_cleanup_owner("fork cleanup", shutdown_fork_owners(), logger)
    except Exception as exc:
        logger.warning(f"shutdown: fork cleanup failed: {exc}")
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


def _fork_task_count() -> int:
    from core.fork_runtime import fork_task_count

    return fork_task_count()


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

    stop_watching_omlx()  # an oMLX exit during shutdown is never a crash
    pending_cancel = await _shutdown_owned_tasks(startup_tasks, _log_shutdown)
    from app_shutdown_runtime import shutdown_devices

    devices_failure, pending_cancel = await shutdown_devices(
        _log_shutdown, pending_cancel, _run_cleanup_owner,
    )
    host_access_failure, host_access_cancel = await _shutdown_host_access(
        _log_shutdown,
    )
    pending_cancel = pending_cancel or host_access_cancel
    fork_failure, fork_cancel = await _shutdown_forks(
        _log_shutdown,
    )
    pending_cancel = pending_cancel or fork_cancel
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

    remaining_forks, fork_count_failure = count_shutdown_owner(
        "fork", _fork_task_count, _log_shutdown,
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
            "forks": remaining_forks,
            "host_access": remaining_host_access,
            "host_processes": remaining_processes,
            "idle_checkpoint_timers": remaining_idle_timers(),
        },
        (
            fork_failure, fork_count_failure, host_access_count_failure,
            process_count_failure, rollup_failure, refresh_failure, harness_failure,
            process_failure, host_access_failure, devices_failure, mcp_failure, handoff_failure,
        ),
        process_failure=process_failure,
        pending_cancel=pending_cancel,
        logger=_log_shutdown,
    )

    from app_shutdown_storage import finish_storage_shutdown
    await finish_storage_shutdown(_log_shutdown, pending_cancel,
                                  run_cleanup_owner=_run_cleanup_owner, shutdown_pool=shutdown_pool)
