"""One startup recovery pass for Turns, FIFO inputs, and restart ingress."""

from __future__ import annotations

import asyncio
import time
import uuid

from infra.db_async import run_in_db
from infra.time import utc_now_iso


def close_out_prior_self_restart() -> None:
    """Adopt the durable restart operation being completed by this process."""
    from automation.self_restart import adopt_for_resume, clear_stale_in_flight
    from infra.restart_authorization import serving_head_sha
    from loguru import logger

    cleared = clear_stale_in_flight(serving_head_sha() or None)
    if cleared:
        logger.info("self-restart: superseded {} stale request(s)", cleared)
    operation_id = adopt_for_resume()
    if operation_id:
        logger.info("self-restart: adopted operation {}", operation_id)


async def _replay_held_webhooks() -> None:
    from automation.restart_webhooks import replay_held_webhooks

    outcome = await replay_held_webhooks()
    retryable = outcome.get("retryable") or []
    if retryable:
        raise RuntimeError(
            f"restart retained {len(retryable)} retryable webhook(s)"
        )


def _complete_restart_operation() -> None:
    from automation import self_restart
    from infra.restart_authorization import serving_head_sha

    operation = self_restart.in_flight_request()
    if operation is None:
        return
    if not self_restart.complete_operation(
        str(operation["id"]), deployed_sha=serving_head_sha() or None,
    ):
        raise RuntimeError("restart operation could not release ingress")


async def run_startup_reconcile(logger) -> tuple[object, tuple[str, ...]]:
    """Time and report the pre-bind reconcile phase."""
    phase_started = time.monotonic()
    logger.info("startup phase: restart continuity reconcile started")
    reconciled = await reconcile_startup_continuity(logger)
    logger.info(
        "startup phase: restart continuity reconcile complete in {:.1f}s",
        time.monotonic() - phase_started,
    )
    return reconciled


async def run_startup_drive(harness_task, recovered, wakes, logger) -> None:
    """Time and report the lifecycle-owned post-bind drive phase."""
    phase_started = time.monotonic()
    logger.info("startup phase: restart continuity drive started")
    try:
        await harness_task
        await drive_startup_continuity(recovered, wakes, logger)
    except asyncio.CancelledError:
        logger.warning("startup phase: restart continuity drive cancelled")
        raise
    except (Exception, BaseExceptionGroup) as exc:
        logger.error(
            "startup phase: restart continuity drive failed after {:.1f}s: {}",
            time.monotonic() - phase_started,
            exc,
        )
        raise
    logger.info(
        "startup phase: restart continuity drive complete in {:.1f}s",
        time.monotonic() - phase_started,
    )


async def reconcile_startup_continuity(logger) -> tuple[object, tuple[str, ...]]:
    """Terminalize stale owners durably, before any serving work begins.

    Only database-bound work belongs here. It runs in front of the port bind
    because checkpoint reconciliation and the rest of startup must observe
    crash-orphaned Turns already terminal. Anything that can call a provider,
    reach the network, or fold a large history belongs in
    ``drive_startup_continuity`` instead, where it cannot gate readiness.
    """
    from automation.managed_process_recovery import reconcile_managed_processes
    from core.turn_core import BOOT_EPOCH
    from infra.llm_usage import reconcile_pending_turn_usage
    from infra.thread_recovery import recover_threads
    from automation.restart_wakes import materialize_pending_registrations

    await run_in_db(close_out_prior_self_restart)
    projected_usage = await run_in_db(reconcile_pending_turn_usage)
    if projected_usage:
        logger.info(
            "startup continuity: projected {} pending Turn usage receipt(s)",
            projected_usage,
        )
    wake_threads = await run_in_db(materialize_pending_registrations)
    process_threads = await run_in_db(reconcile_managed_processes)
    recovered = await run_in_db(
        recover_threads,
        boot_epoch=BOOT_EPOCH,
        recovered_at=utc_now_iso(),
        successor_id=lambda _thread_id: str(uuid.uuid4()),
    )
    pending_switches = await run_in_db(_pending_switch_threads)
    await run_in_db(_complete_restart_operation)
    return recovered, tuple(
        dict.fromkeys((*wake_threads, *process_threads, *pending_switches))
    )


def _pending_switch_threads() -> tuple[str, ...]:
    """Pending selections without FIFO claims also need the existing startup wake."""
    from infra.database import get_db

    db = get_db()
    try:
        return tuple(str(row[0]) for row in db.execute(
            "SELECT id FROM threads WHERE switch_status IN ('preparing','ready') "
            "AND switch_operation_id IS NOT NULL ORDER BY id",
        ))
    finally:
        db.close()


async def _drive_recovered_item(label, action, logger):
    """Report a recovered item's failure without cancelling its siblings."""
    from infra.send_types import SendRejected

    try:
        await action
    except SendRejected as exc:
        logger.warning("startup continuity: {} was not delivered: {}", label, exc.detail)
    except Exception:
        logger.exception("startup continuity: {} failed", label)
        raise


async def drive_startup_continuity(recovered, wake_threads, logger) -> None:
    """Drive independent conversations; their durable claims preserve FIFO order."""
    from infra.thread_execution import drive_terminal_settlement, drive_thread
    from infra.thread_input_types import HeldAdmission
    from infra.turn_admission_driver import drive_turn_admission

    claimed_threads = {admission.thread_id for admission in recovered.batches}
    claimed_threads.update(item.thread_id for item in recovered.settlements)
    actions = [
        *((f"settlement for {item.thread_id}", drive_terminal_settlement(item))
          for item in recovered.settlements),
        *((f"Turn admission for {item.thread_id}", drive_turn_admission(item))
          for item in recovered.batches),
        *((f"wake for {thread_id}", drive_thread(thread_id))
          for thread_id in wake_threads if thread_id not in claimed_threads),
        ("held webhooks", _replay_held_webhooks()),
    ]
    results = await asyncio.gather(
        *(_drive_recovered_item(label, action, logger) for label, action in actions),
        return_exceptions=True,
    )
    errors = [result for result in results if isinstance(result, BaseException)]
    if errors:
        raise BaseExceptionGroup("startup continuity failures", errors)
    logger.info(
        "startup continuity: settlements={} admissions={} held={}",
        len(recovered.settlements),
        sum(not isinstance(item, HeldAdmission) for item in recovered.batches),
        sum(isinstance(item, HeldAdmission) for item in recovered.batches),
    )
