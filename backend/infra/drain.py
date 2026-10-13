"""Centralized drain logic — coordinate graceful shutdown."""

from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager
from contextvars import ContextVar
import threading
import time

from loguru import logger

from core.turn_core import active_turn_blockers, active_turn_count
from host_access.processes import PROCESS_OWNER
from host_access.supervisor import host_access_supervisor_count
from infra import state
from infra.config import RUNTIME_DIR

SIGNAL_FILE = RUNTIME_DIR / "corsia-restart.signal"
DRAIN_HARD_CAP = 300  # 5 minutes max drain time
_drain_admission_lock = threading.Lock()
_turn_admissions = 0


class _TurnAdmission:
    __slots__ = ("live",)

    def __init__(self) -> None:
        self.live = True


_inherited_turn_admission: ContextVar[_TurnAdmission | None] = ContextVar(
    "inherited_turn_admission",
    default=None,
)


def _active_work_counts() -> dict[str, int]:
    with _drain_admission_lock:
        turn_admissions = _turn_admissions
    counts = {
        "turnAdmissions": turn_admissions,
        "activeTurns": active_turn_count(),
        "hostAccessSupervisors": host_access_supervisor_count(),
        # Only groups still running are work worth waiting for. A group left in
        # death_unverified has already been sent TERM and KILL, so no further
        # in-process action can retire it; counting it here made drain readiness
        # permanently false and wedged every later stop, restart and update on
        # that machine. Shutdown still tracks it through active_process_count.
        "ownedProcessGroups": len(PROCESS_OWNER.running_records()),
        "browserCommands": PROCESS_OWNER.active_browser_command_count(),
    }
    counts["activeProcesses"] = sum(counts.values())
    return counts


def start_drain_projection(reason: str) -> dict:
    """Project an already-durable restart operation to clients and scripts."""
    with _drain_admission_lock:
        state._drain = {
            "reason": reason,
            "started_at": time.time(),
        }
    # Keep the compatibility projection in service-owned runtime state. The
    # backend sandbox deliberately blocks writes to the historical /tmp path.
    SIGNAL_FILE.parent.mkdir(parents=True, exist_ok=True)
    SIGNAL_FILE.write_text(reason)
    logger.info(f"Drain started: {reason}")

    # Broadcast to all connected clients. blockingTurns names other users'
    # thread titles and ids, which broadcast_all cannot scope per recipient,
    # so the fan-out omits it; each client's own authenticated poll of
    # GET /api/system/drain fills in its own filtered blockingTurns.
    status = get_drain_status()
    state.broadcast_all(
        {
            "type": "system_draining",
            "draining": True,
            **{k: v for k, v in status.items() if k != "blockingTurns"},
            "blockingTurns": [],
        },
    )
    return status


def get_drain_status(*, user_id: str | None = None) -> dict:
    """Return durable restart truth with process-local readiness observations.

    ``user_id`` scopes ``blockingTurns`` to that user's own threads. Every
    caller that returns this to an authenticated HTTP request must pass its
    own ``user_id``; omitting it is only safe for internal, single-operator
    bookkeeping that never crosses a user boundary.
    """
    from automation.self_restart import in_flight_request

    operation = in_flight_request()
    if operation is None:
        return {
            "active": False,
            "reason": "",
            "activeProcesses": 0,
            "turnAdmissions": 0,
            "activeTurns": 0,
            "hostAccessSupervisors": 0,
            "ownedProcessGroups": 0,
            "browserCommands": 0,
            "blockingTurns": [],
        }
    with _drain_admission_lock:
        drain_state = state._drain
        started_at = (
            drain_state["started_at"]
            if drain_state is not None
            else None
        )
    if started_at is None:
        from datetime import datetime

        cutoff = operation.get("cutoff_at") or operation["requested_at"]
        parsed = datetime.fromisoformat(str(cutoff))
        started_at = parsed.timestamp()
    counts = _active_work_counts()
    elapsed = time.time() - started_at
    return {
        "active": True,
        "operation_id": str(operation["id"]),
        "phase": operation.get("phase"),
        "reason": operation.get("reason") or "",
        **counts,
        "blockingTurns": active_turn_blockers(user_id=user_id) if user_id is not None else active_turn_blockers(),
        "elapsed": round(elapsed),
        "hard_cap_remaining": max(0, round(DRAIN_HARD_CAP - elapsed)),
    }


def get_drain_operation_status(operation_id: str) -> dict | None:
    """Project one durable operation without replacing global drain status."""
    from automation.self_restart import operation_by_id

    operation = operation_by_id(operation_id)
    if operation is None:
        return None
    return {
        "operation_id": str(operation["id"]),
        "status": operation["status"],
        "phase": operation.get("phase"),
        "error": operation.get("error"),
        "kind": operation.get("kind"),
        "trigger": operation.get("trigger"),
        "target_version": operation.get("target_version"),
        "drain_total": operation.get("drain_total"),
        "failure_code": operation.get("failure_code"),
        "failure_detail": operation.get("failure_detail"),
        "conductor_told_at": operation.get("conductor_told_at"),
    }


def check_drain_ready() -> bool:
    """Return True when no active automated processes or user turns.

    Readiness reads the same durable operation as ``get_drain_status``. A
    process-local flag cannot be the source of truth here: it is empty after a
    restart while the durable operation survives, so the two would disagree
    exactly when continuity matters, and readiness would report "not draining"
    for a restart that is genuinely in flight.
    """
    from automation.self_restart import in_flight_request

    operation = in_flight_request()
    if operation is None:
        return False
    with _drain_admission_lock:
        if _turn_admissions:
            return False

    counts = _active_work_counts()
    if counts["activeProcesses"] != 0:
        return False

    # Re-read both fences after counting: an admission or a replacement
    # operation between the count and here means this answer is already stale.
    current = in_flight_request()
    if current is None or current.get("id") != operation.get("id"):
        return False
    with _drain_admission_lock:
        return _turn_admissions == 0


def cancel_drain_projection() -> None:
    """Clear only process-local/client compatibility projection state."""
    with _drain_admission_lock:
        state._drain = None
    SIGNAL_FILE.unlink(missing_ok=True)
    logger.info("Drain cancelled")

    state.broadcast_all(
        {
            "type": "system_draining",
            "draining": False,
        },
    )


def is_draining() -> bool:
    """Return canonical durable restart-hold truth."""
    from automation.self_restart import restart_holds_new_work

    return restart_holds_new_work()


def begin_turn_admission() -> _TurnAdmission:
    """Own ingress accounting while durable admission decides hold versus delivery."""
    global _turn_admissions
    with _drain_admission_lock:
        _turn_admissions += 1
        return _TurnAdmission()


def finish_turn_admission(admission: _TurnAdmission) -> None:
    """Release one send admission after dispatch owns or rejects it."""
    global _turn_admissions
    with _drain_admission_lock:
        if not admission.live:
            raise RuntimeError("turn admission already finished")
        if _turn_admissions <= 0:
            raise RuntimeError("turn admission accounting underflow")
        admission.live = False
        _turn_admissions -= 1


def turn_admission_is_inherited() -> bool:
    """Return whether this task belongs to pre-drain admitted ingress."""
    admission = _inherited_turn_admission.get()
    with _drain_admission_lock:
        return admission is not None and admission.live


@contextmanager
def inherit_turn_admission(admission: _TurnAdmission) -> Iterator[None]:
    """Let child work consume the admission already owned by its ingress."""
    with _drain_admission_lock:
        if not admission.live or _turn_admissions <= 0:
            raise RuntimeError("inherited turn admission has no live owner")
    token = _inherited_turn_admission.set(admission)
    try:
        yield
    finally:
        _inherited_turn_admission.reset(token)
