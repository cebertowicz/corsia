"""Drives that release a harness account-limit scope once its reset has passed.

The deadline is the limited verdict's own ``resets_at`` and its release marker
(``connections.usage_verdicts.release_due``), so there is no schedule to arm,
cancel or reconstruct:

- ``drive_limit_releases(now)`` is the continuation sweep's system phase. It
  reads each connection's usage once and drives every due scope, which is also
  what recovers a release across a restart.
- ``schedule_scope_release(subject)`` drives every held head in a scope after
  an allowed or sign-in verdict commits, through ``infra.app_loop``.
- ``schedule_limited_heads(thread_ids)`` drives the heads a committed rollover
  opt-in may now move, the same way.
"""

from __future__ import annotations

import logging
import uuid
from dataclasses import dataclass
from datetime import datetime, timezone

from connections.usage_verdicts import DueRelease, LimitSubject
from infra.connection_limit_release import LimitProbe, candidate_rows
from infra.db_async import run_in_db
from infra.product_events import emit_product_event

log = logging.getLogger(__name__)
_PROBE_THREADS: dict[str, "ProbeCounter"] = {}


@dataclass(frozen=True, slots=True)
class Started:
    thread_id: str


@dataclass(frozen=True, slots=True)
class NoEligibleHead:
    """The candidate query found no head to probe: real no-work."""


@dataclass(frozen=True, slots=True)
class Held:
    """Admission still holds the head, so its account is still limited."""

    thread_id: str
    reason: str | None


@dataclass(frozen=True, slots=True)
class Failed:
    """Every candidate raised or failed plan admission; ``thread_id`` owns a started marker."""

    error: str
    thread_id: str | None = None


ProbeOutcome = Started | NoEligibleHead | Held | Failed


@dataclass(frozen=True, slots=True)
class ProbeCounter:
    connection: str
    reset_window: str
    source: str
    account_id: str | None = None


def _record_probe_start(thread_id: str, probe: LimitProbe) -> None:
    _PROBE_THREADS[thread_id] = ProbeCounter(
        probe.subject.provider, probe.resets_at, probe.source, probe.subject.account_id,
    )
    emit_product_event(
        "account_limit_reset_probe", "turn_started", thread_id=thread_id,
        properties={
            "counter": "account_limit_reset_probe_turns_total",
            "connection": probe.subject.provider,
            "account_id": probe.subject.account_id,
            "reset_window": probe.resets_at,
            "source": probe.source,
        },
    )


def record_probe_outcome(thread_id: str | None, outcome: str | None) -> None:
    """Count a process-local probe outcome without making it delivery state."""
    probe = _PROBE_THREADS.pop(thread_id, None) if thread_id is not None else None
    if probe is None or outcome is None:
        return
    emit_product_event(
        "account_limit_reset_probe", "outcome", thread_id=thread_id,
        properties={
            "counter": "account_limit_reset_probe_outcomes_total",
            "connection": probe.connection,
            "account_id": probe.account_id,
            "outcome": outcome,
            "source": probe.source,
        },
    )


def _error_text(error: BaseException) -> str:
    return f"{type(error).__name__}: {error}"


async def _drive_one_probe(probe: LimitProbe) -> ProbeOutcome:
    """Claim the oldest eligible head as the window's one probe Turn.

    The claim, its gates and the ``started`` marker share one write
    (``start_idle_turn``), so a lost race claims nothing and moves on.
    """
    from core.turn_core import BOOT_EPOCH
    from infra.thread_input_types import FailedInputBatch, HeldAdmission
    from infra.thread_inputs import start_idle_turn
    from infra.turn_admission_driver import drive_turn_admission
    from infra.time import utc_now_iso

    held: Held | None = None
    failure: Failed | None = None
    after = None
    while True:
        page = await run_in_db(candidate_rows, probe.subject, after=after)
        for thread_id, _model, _head_key in page:
            try:
                admission = await run_in_db(
                    start_idle_turn, thread_id, turn_id=str(uuid.uuid4()),
                    boot_epoch=BOOT_EPOCH, started_at=utc_now_iso(),
                    connection_limit_probe=probe,
                )
            except Exception as error:
                log.exception("connection-limit %s probe failed for thread %s", probe.source, thread_id)
                failure = Failed(_error_text(error))
                continue
            if admission is None:
                continue
            if isinstance(admission, HeldAdmission):
                held = held or Held(thread_id, admission.reason)
            elif isinstance(admission, FailedInputBatch):
                failure = Failed(admission.error_text)
            else:
                _record_probe_start(thread_id, probe)
            try:
                await drive_turn_admission(admission)
            except Exception as error:
                log.exception("connection-limit probe drive failed for thread %s", thread_id)
                if not isinstance(admission, (HeldAdmission, FailedInputBatch)):
                    return Failed(_error_text(error), thread_id)
            if not isinstance(admission, (HeldAdmission, FailedInputBatch)):
                return Started(thread_id)
        if page.next_after is None:
            return failure or held or NoEligibleHead()
        after = page.next_after


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _write_marker(
    due: DueRelease, state: str, *, error: str | None = None,
    owner_thread_id: str | None = None,
) -> bool:
    from connections.harness_usage import write_release

    return write_release(
        due.subject, due.resets_at, state, at=_now(), error=error,
        owner_thread_id=owner_thread_id,
    )


async def drive_scope_release(due: DueRelease, *, source: str = "sweep") -> ProbeOutcome | None:
    """Drive one due scope: probe it, or settle its heads once its release is exhausted.

    ``NoEligibleHead`` and ``Failed`` are recorded on the marker; ``Held``
    writes nothing, because the refusal it proves records a fresh verdict.
    """
    if due.exhausted:
        await settle_exhausted_release(due)
        return None
    try:
        outcome = await _drive_one_probe(LimitProbe(due.subject, due.resets_at, source))
    except Exception as error:  # C5: the claim and its marker rolled back together.
        log.exception("connection-limit release failed for %s", due.subject)
        outcome = Failed(_error_text(error))
    if isinstance(outcome, NoEligibleHead):
        await run_in_db(_write_marker, due, "no_eligible_head")
    elif isinstance(outcome, Failed):
        await run_in_db(
            _write_marker, due, "failed", error=outcome.error, owner_thread_id=outcome.thread_id,
        )
    return outcome


def _settle_exhausted(due: DueRelease, db=None) -> tuple | None:
    """Fail every held head of an exhausted window and mark it, in one write."""
    from connections.harness_usage import write_release
    from infra.database import write_transaction
    from infra.thread_inputs import fail_claimable_prefix

    error = due.reason or "provider usage limit reached"
    with write_transaction(db, scope="usage_release_exhausted") as connection:
        if not write_release(due.subject, due.resets_at, "exhausted", at=_now(), db=connection):
            return None
        failed = []
        after = None
        while True:
            page = candidate_rows(due.subject, after=after, db=connection)
            for thread_id, _model, _head_key in page:
                if batch := fail_claimable_prefix(connection, thread_id, error=error):
                    failed.append(batch)
            if page.next_after is None:
                return tuple(failed)
            after = page.next_after


async def settle_exhausted_release(due: DueRelease) -> None:
    """C6: a window whose release kept failing stops being driven, visibly."""
    from infra.turn_admission_driver import drive_turn_admission

    failed = await run_in_db(_settle_exhausted, due)
    if failed is None:
        return
    log.error(
        "connection-limit release exhausted for %s window %s: %d held threads failed",
        due.subject, due.resets_at, len(failed),
    )
    emit_product_event(
        "usage_limit", "release_exhausted",
        properties={
            "account_key": due.subject.provider, "account_id": due.subject.account_id,
            "scope_key": due.subject.scope_key,
            "resets_at": due.resets_at, "failed_threads": len(failed),
        },
    )
    for batch in failed:
        await drive_turn_admission(batch)


def _due_releases(now: datetime) -> list[DueRelease]:
    from automation.continuation.admission import DEFERRAL_BASE_SECONDS, MAX_DEFERRALS
    from connections.harness_usage import stored_usage
    from connections.usage_verdicts import release_due
    from connections.vendor_accounts import list_accounts
    from connections.account_evidence import readiness_reason
    from connections.account_receipts import account_receipt, limit_subject
    from infra.database import get_db

    due: list[DueRelease] = []
    db = get_db()
    try:
        for account in list_accounts(db=db):
            provider = account['provider']
            if provider not in ('codex', 'claude-code') or readiness_reason(account) is not None:
                continue
            receipt = account_receipt(account)
            if receipt is None:
                continue
            usage = stored_usage(provider=provider, account_id=account['id'], db=db)
            due.extend(release_due(
                usage, now, account=limit_subject(provider, 'null', receipt=receipt),
                max_attempts=MAX_DEFERRALS, backoff_seconds=DEFERRAL_BASE_SECONDS,
            ))
    finally:
        db.close()
    return due


async def drive_limit_releases(now: datetime) -> int:
    """The sweep's system phase: drive every stored limit whose reset has passed.

    It reads only stored verdicts, never vendor state. One scope's failure is
    recorded on its marker and never stops the others. A window driven for the
    first time has just stopped reading limited, so the catalog is pushed once
    for it; a retry or an exhausted window changes nothing a reader shows.
    """
    due = await run_in_db(_due_releases, now)
    if any(item.attempts == 0 and not item.exhausted for item in due):
        from infra.state.ws import notify_models_changed

        notify_models_changed()
    for item in due:
        try:
            await drive_scope_release(item)
        except Exception:
            log.exception("connection-limit release phase failed for %s", item.subject)
    return len(due)


async def _drive_scope_batch(subject: LimitSubject) -> None:
    """Drive every matching idle prefix independently after an allowed commit."""
    from infra.thread_execution import drive_idle_thread

    after = None
    while True:
        page = await run_in_db(candidate_rows, subject, after=after)
        for thread_id, _model, _head_key in page:
            try:
                await drive_idle_thread(thread_id, connection_limit_subject=subject)
            except Exception:
                log.exception("connection-limit release failed for thread %s", thread_id)
        if page.next_after is None:
            return
        after = page.next_after


def schedule_scope_release(subject: LimitSubject) -> None:
    """Hand an allowed/sign-in release to the app loop from any thread.

    Raises ``app_loop.AppLoopUnavailable`` outside the app's lifetime; the
    after-commit runner logs that at ERROR, and startup recovery drives the heads.
    """
    import infra.app_loop as app_loop

    app_loop.submit(_drive_scope_batch(subject))


async def _drive_limited_heads(thread_ids: list[str]) -> None:
    """Offer each limit-held thread to the idle driver, which alone decides a move."""
    from infra.thread_execution import drive_idle_thread

    for thread_id in thread_ids:
        try:
            await drive_idle_thread(thread_id)
        except Exception:
            log.exception("policy-change drive failed for thread %s", thread_id)


def schedule_limited_heads(thread_ids: list[str]) -> None:
    """Hand threads held on a limit to the app loop after a rollover opt-in commits.

    Raises ``app_loop.AppLoopUnavailable`` outside the app's lifetime; the
    after-commit runner logs that at ERROR.
    """
    import infra.app_loop as app_loop

    app_loop.submit(_drive_limited_heads(thread_ids))


async def _stop_at_plan_limit(thread_id: str, turn_id: str) -> None:
    """One isolated system Stop; a failure is logged with its Turn, never raised."""
    from ws.stop_control import stop_turn_for_cause

    try:
        if await run_in_db(_turn_stop_current, turn_id):
            await stop_turn_for_cause(thread_id, turn_id)
    except Exception:
        log.exception("plan-limit stop of %s turn %s failed", thread_id, turn_id)


def _turn_stop_current(turn_id):
    from connections.account_receipts import limit_subject, subject_is_current
    from infra.connection_limit_release import model_connection
    from infra.database import get_db
    from infra.vendor_login import read_turn_receipt

    db = get_db()
    try:
        row = db.execute('SELECT model FROM active_turns WHERE id=?', (turn_id,)).fetchone()
        if row is None:
            return True  # The exact-Turn Stop owner handles an already absent Turn.
        provider = model_connection(row['model'], db)
        if provider is None:
            return False
        receipt = read_turn_receipt(turn_id, db=db)
        return receipt is not None and subject_is_current(limit_subject(provider, 'null', receipt=receipt), db=db)
    finally:
        db.close()


def schedule_plan_limit_stops(turns: tuple[tuple[str, str], ...]) -> None:
    """Hand one tracked Stop per exact running Turn to the app loop; never awaited.

    Raises ``app_loop.AppLoopUnavailable`` outside the app's lifetime; the
    after-commit runner logs that at ERROR.
    """
    import infra.app_loop as app_loop

    for thread_id, turn_id in turns:
        app_loop.submit(_stop_at_plan_limit(thread_id, turn_id))
