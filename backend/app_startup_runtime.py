"""Startup of runtime services after MCP discovery and durable recovery."""

import asyncio
import sys

from events.connectors.github_notifications import notifications_poll_loop
from events.connectors.github_pr_quiet_watch import pr_quiet_watch_loop
from events.connectors.mail_poller import mail_poll_loop
from infra import state
from infra.cpu_sampler import apply_cpu_profiler, stop_cpu_sampler
from infra.otel_logs.lifecycle import start_corsia_logs, stop_corsia_logs
from jobs.nightly import nightly_job_loop
from library.producer import library_refresh_loop
from subagents.cc_idle_retirement import idle_retirement_loop
from thread.idle_checkpoint import rearm_idle_checkpoints


# sqlite3 releases the GIL for each fetched row, so contended fetches otherwise
# wait a full Python thread-switch interval at every handoff.
SQLITE_FETCH_SWITCH_INTERVAL_SECONDS = 0.0001


def configure_sqlite_fetch_switch_interval() -> None:
    """Reduce GIL handoff latency before database workers begin running."""
    sys.setswitchinterval(SQLITE_FETCH_SWITCH_INTERVAL_SECONDS)


#: Every background loop this module starts, as (shutdown label, ``state``
#: attribute). The lifespan cancels and joins exactly these at shutdown.
RUNTIME_LOOP_TASKS: tuple[tuple[str, str], ...] = (
    ("idle checkpoint recovery", "_idle_recovery_task"),
    ("nightly job", "_nightly_job_task"),
    ("network health", "_network_health_task"),
    ("turn-buffer sweeper", "_turn_buffer_sweeper"),
    ("plan liveness", "_plan_liveness_task"),
    ("presence sweep", "_presence_sweep_task"),
    ("cc idle retirement", "_cc_idle_retirement_task"),
    ("github notifications", "_github_notifications_task"),
    ("mail poller", "_mail_poller_task"),
    ("library refresh", "_library_refresh_task"),
    ("pr quiet watch", "_pr_quiet_watch_task"),
    ("request timing flush", "_request_timing_flush_task"),
    ("live bulletin feed", "_live_bulletin_task"),
)


async def resume_local_setup() -> None:
    """Restore managed-model state and resume durable setup work after a restart.

    Each step is independent and none can stop the app: a failure is logged and
    local inference reports its own state.
    """
    import logging

    from infra.db_async import run_in_db
    from infra.failure_reference import new_reference
    from llm.local_setup import record_resume_failure
    from llm.local_model import backfill_legacy_record
    from llm.local_setup_flow import resume_pending_setup
    from llm.local_setup_runtime import rename_managed_server
    from onboarding.local_download_wake import resume_pending_local_download

    steps = (
        ("name the managed server", lambda: run_in_db(rename_managed_server)),
        ("record an earlier setup's model", lambda: asyncio.to_thread(backfill_legacy_record)),
        ("resume local setup", resume_pending_setup),
        ("resume local download notice", resume_pending_local_download),
    )
    for label, step in steps:
        try:
            await step()
        except Exception:
            ref = new_reference("local")
            logging.getLogger(__name__).warning("Local setup startup step failed (%s) reference=%s", label, ref, exc_info=True)
            if label in {"record an earlier setup's model", "resume local setup", "resume local download notice"}:
                record_resume_failure(ref)


async def _startup_harness_discovery() -> list[dict]:
    """Re-ask Claude/Codex for their catalogs and sign-in once at backend startup."""
    from connections.startup import startup_harness_discovery

    return await startup_harness_discovery()


async def _cleanup_terminal_lease_processes(lease: dict) -> None:
    """Terminate exactly the process groups owned by one terminal lease."""
    from host_access.processes import PROCESS_OWNER

    await PROCESS_OWNER.terminate_lease(
        str(lease["id"]),
        reason=f"lease_{lease['state']}",
    )


#: Upper bound on how long startup waits for the own-login lookup; the lookup
#: itself times out at 2 s, so this only guards a wedged worker thread.
_OWN_LOGIN_STARTUP_WAIT_SECONDS = 3.0


async def _start_github_ownership_refresh() -> None:
    """Resolve GitHub ownership identity before any event is routed.

    The server accepts webhooks only after startup completes, and the
    notifications poller starts below, so a bounded wait here means the
    first routed PR event already sees the login when ``gh`` is reachable.
    """
    from events.connectors.github_ownership import refresh_own_login

    task = asyncio.create_task(asyncio.to_thread(refresh_own_login))
    state._github_ownership_login_task = task
    await asyncio.wait({task}, timeout=_OWN_LOGIN_STARTUP_WAIT_SECONDS)


def stop_runtime_logs(logger) -> None:
    """Stop diagnostics, then detach log capture before storage teardown."""
    stop_cpu_sampler()
    stop_corsia_logs(logger)


def _apply_saved_cpu_profiler() -> None:
    """Start the sampler at boot when the saved flag (or the env override) asks."""
    from infra.settings import get_feature_flags

    apply_cpu_profiler(bool(get_feature_flags().get("cpu_profiler", False)))


def _report_classifier_download_failure(state: dict) -> None:
    """Tell the owner, through the existing classifier failure notice, when a startup download ends with nothing installed.

    An installed version keeps serving and is noticed by the worker if it is damaged, so a failure then stays a warning.
    """
    import infra.app_loop as app_loop
    from automation.update_failure_notice import tell_conductor_about_corrections_classifier_failure
    from core.corrections_asset_files import newest_pin
    from jobs.corrections_assets import installed

    if installed():
        return
    try:
        app_loop.submit(tell_conductor_about_corrections_classifier_failure(
            newest_pin(), str(state.get("code") or "download failed"), str(state.get("reason") or "The download failed."),
        ))
    except app_loop.AppLoopUnavailable:
        from loguru import logger

        logger.warning("Corrections classifier failure notice not sent: no live event loop")


def _start_current_corrections_classifier() -> None:
    """Carry on a download setup asked for, and adopt a missing pinned classifier once the first account exists.

    Disposable test runs fetch nothing unless they point the flight at their own release host.
    """
    import os

    from core.corrections_asset_download import BASE_URL_ENV
    from core.corrections_asset_download import FLIGHT as classifier_download
    from core.corrections_asset_files import current_asset_verified
    from infra import accounts

    if os.environ.get("CORSIA_TEST_MODE") == "1" and not os.environ.get(BASE_URL_ENV):
        return
    classifier_download.resume(on_failure=_report_classifier_download_failure)
    if accounts.has_users() and not current_asset_verified():
        classifier_download.start(on_failure=_report_classifier_download_failure)


def _schedule_current_corrections_classifier() -> None:
    """Reconcile off the startup path: verifying the installed asset hashes its files, and nothing waits on the result."""
    import infra.app_loop as app_loop

    async def reconcile() -> None:
        await asyncio.to_thread(_start_current_corrections_classifier)

    app_loop.submit(reconcile())


async def _start_runtime_services() -> None:
    # The database is initialized by now; diagnostics and capture start before
    # the services below so their startup lines reach the conductor log store.
    await asyncio.to_thread(_apply_saved_cpu_profiler)
    await asyncio.to_thread(start_corsia_logs)
    await _start_github_ownership_refresh()
    try:
        from llm.network_discovery import health_sweep_loop

        state._network_health_task = asyncio.create_task(health_sweep_loop())
    except Exception as exc:
        from loguru import logger

        logger.warning(f"Health sweep startup failed: {exc}")
    from automation.continuation.recovery import startup_recover

    await startup_recover()
    from core.checkpoint_runtime import reconcile_startup_checkpoints

    await reconcile_startup_checkpoints()
    from core.artifact_review_capture import reconcile_startup_reviews

    await reconcile_startup_reviews()
    from connections.mailhost.check import settle_after_restart

    await settle_after_restart()
    from core import placement_worker

    placement_worker.poke()
    state._idle_recovery_task = asyncio.create_task(rearm_idle_checkpoints())
    state._nightly_job_task = asyncio.create_task(nightly_job_loop())
    from core.corrections_asset_download import recover_interrupted_install
    from core.corrections_asset_signals import set_corrections_waker
    from jobs.corrections_realtime import startup_scan

    set_corrections_waker(startup_scan)
    recover_interrupted_install()
    _schedule_current_corrections_classifier()
    startup_scan()
    from automation.plan_liveness import plan_liveness_sweep_loop

    state._plan_liveness_task = asyncio.create_task(plan_liveness_sweep_loop())
    from ws.presence_control import presence_sweep_loop

    state._presence_sweep_task = asyncio.create_task(presence_sweep_loop())
    state._cc_idle_retirement_task = asyncio.create_task(idle_retirement_loop())
    state._github_notifications_task = asyncio.create_task(notifications_poll_loop())
    state._mail_poller_task = asyncio.create_task(mail_poll_loop())
    state._library_refresh_task = asyncio.create_task(library_refresh_loop())
    state._pr_quiet_watch_task = asyncio.create_task(pr_quiet_watch_loop())
    from infra.request_timing import start_flush_loop

    state._request_timing_flush_task = start_flush_loop()
    from infra.bulletin_live import live_bulletin_loop

    state._live_bulletin_task = asyncio.create_task(live_bulletin_loop())
    from infra.state import turn_buffers as state_turn_buffers

    state._turn_buffer_sweeper = state_turn_buffers.start_sweeper()
    from host_access.supervisor import (
        configure_host_access_terminal_callback,
        recover_host_access_supervisors,
    )

    configure_host_access_terminal_callback(_cleanup_terminal_lease_processes)
    from automation.managed_process_events import install as install_process_events

    install_process_events()
    await recover_host_access_supervisors()
