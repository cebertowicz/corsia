"""Crash-safe owner for independently reviewed temporary host access."""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
import inspect
import json
from pathlib import Path
import time
from typing import Any, Awaitable, Callable

from loguru import logger

from host_access.errors import HostAccessSupervisorError
from host_access.failure_reasons import (
    HostAccessBrowserIsolationError,
    lease_failure_reason,
)
from host_access.redaction import validate_action_descriptor
from host_access.refusal_messages import exception_chain_summary, single_line
from host_access.review_records import application_review_record
from host_access import reviewer_target
from host_access.reviewer_target import ReviewerBinding
from infra.db_async import run_in_db
from infra.host_access_leases import (
    ACTION_PERMIT_TTL_SECONDS,
    NONTERMINAL_STATES,
    LeaseConflictError,
    LeaseNotFoundError,
    activate_lease,
    approve_lease,
    block_lease,
    get_lease,
    get_open_lease_for_session,
    lease_has_policy_exception,
    list_leases,
    list_persisted_nonterminal_leases,
    mark_action_permit_consumed,
    record_action_review,
    record_activation_wake_delivery,
    record_application_review,
    reject_lease,
    release_lease,
    request_lease,
    revoke_lease,
    terminate_lease_projection,
)
from infra.settings import get_host_access_auto_approve
from infra.time import utc_now_iso
from tools.sandbox_config import registered_repo_for_path, repo_worktree_root


class ProjectPolicyChangedError(HostAccessSupervisorError):
    """The immutable lease no longer matches current repository policy."""


class PolicyExceptionScreenError(HostAccessSupervisorError):
    """A pre-review repository-policy screen denied one action."""


class ActionReviewUnavailableError(HostAccessSupervisorError):
    """The reviewer produced no verdict, so this one action failed closed.

    Raised when the reviewer infrastructure (target resolution, transport,
    model) fails before or during a verdict. The action is denied, but the
    armed lease stays live so the next attempt runs a fresh review. Never
    raised on a real verdict, revocation, expiry, policy change, or owner
    health loss — those keep their terminal behavior.
    """


def failure_detail(exc: BaseException, *, limit: int = 300) -> str:
    """One bounded, single-line, model-visible reason for a review failure.

    Collapses whitespace and control characters (so the surfaced text can
    never be multi-line), truncates to ``limit`` characters, and prefixes the
    exception class name. Every host-access surface that shows an internal
    failure to the holder must route through this sanitizer.
    """
    return f"{type(exc).__name__}: {single_line(str(exc), limit) or 'no detail'}"



@dataclass(frozen=True, slots=True)
class _OwnerPermit:
    permit_id: str
    thread_id: str
    lease_revision: int
    application_hash: str
    tool_name: str
    action_hash: str
    monotonic_deadline: float


@dataclass(slots=True)
class _LeaseOwner:
    lease_id: str
    application_hash: str
    reviewer_model: str
    reviewer_binding: ReviewerBinding
    ready: asyncio.Future[None]
    root_thread_id: str = ""
    user_id: str = ""
    application_json: str = "{}"
    public_lease_json: str = "{}"
    approval_revision: int | None = None
    active_revision: int | None = None
    expires_at: datetime | None = None
    monotonic_deadline: float | None = None
    lineages: dict[str, tuple[str, ...]] = field(default_factory=dict)
    permits: dict[str, _OwnerPermit] = field(default_factory=dict)
    terminal_task: asyncio.Task[dict[str, Any]] | None = None
    activated: asyncio.Event = field(default_factory=asyncio.Event)
    review_lock: asyncio.Lock = field(default_factory=asyncio.Lock)
    task: asyncio.Task[None] | None = None
    wake_retry_task: asyncio.Task[None] | None = None
    graceful: bool = False
    stopping: bool = False


_owners: dict[str, _LeaseOwner] = {}
_application_locks: dict[str, asyncio.Lock] = {}
_activation_locks: dict[str, asyncio.Lock] = {}
_terminal_callback: Callable[[dict[str, Any]], Any] | None = None


def _encoded(value: dict[str, Any]) -> str:
    return json.dumps(
        value,
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
    )


def _decoded(value: str) -> dict[str, Any]:
    result = json.loads(value)
    if not isinstance(result, dict):
        raise HostAccessSupervisorError("host-access owner snapshot is invalid")
    return result


def _as_utc(value: str) -> datetime:
    parsed = datetime.fromisoformat(value)
    if parsed.tzinfo is None:
        return parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def _owner_public(owner: _LeaseOwner) -> dict[str, Any]:
    return _decoded(owner.public_lease_json)


def _owner_application(owner: _LeaseOwner) -> dict[str, Any]:
    return _decoded(owner.application_json)


def _owner_is_active(owner: _LeaseOwner) -> bool:
    deadline = owner.monotonic_deadline
    return bool(
        owner.activated.is_set()
        and owner.active_revision is not None
        and deadline is not None
        and time.monotonic() < deadline
        and supervisor_is_healthy(owner.lease_id)
    )


def _select_owner(thread_id: str) -> _LeaseOwner | None:
    candidates = [
        owner
        for owner in _owners.values()
        if thread_id in owner.lineages and _owner_is_active(owner)
    ]
    if not candidates:
        return None
    return min(candidates, key=lambda owner: len(owner.lineages[thread_id]))



def configure_host_access_terminal_callback(
    callback: Callable[[dict[str, Any]], Any] | None,
) -> None:
    """Install Task 6's one process-cleanup callback."""
    global _terminal_callback
    _terminal_callback = callback


def host_access_supervisor_count() -> int:
    return sum(supervisor_is_healthy(lease_id) for lease_id in tuple(_owners))


def supervisor_is_healthy(lease_id: str) -> bool:
    owner = _owners.get(lease_id)
    task = owner.task if owner is not None else None
    return bool(
        owner
        and task
        and not owner.graceful
        and not owner.stopping
        and not task.done()
        and task.cancelling() == 0
    )


async def _snapshot_canonical_lineages(
    *,
    root_thread_id: str,
    user_id: str,
) -> dict[str, tuple[str, ...]]:
    """Freeze current same-user descendants from canonical ``spawned_by``."""

    def load() -> list[tuple[str, str | None]]:
        from infra.database import get_db

        db = get_db()
        try:
            root = db.execute(
                "SELECT user_id FROM threads WHERE id=?",
                (root_thread_id,),
            ).fetchone()
            if root is None or root["user_id"] != user_id:
                raise HostAccessSupervisorError(
                    "host-access root identity is unavailable"
                )
            rows = db.execute(
                "SELECT id,spawned_by FROM threads WHERE user_id=? ORDER BY id",
                (user_id,),
            ).fetchall()
            return [(str(row["id"]), row["spawned_by"]) for row in rows]
        finally:
            db.close()

    rows = await run_in_db(load)
    children: dict[str, list[str]] = {}
    for thread_id, parent in rows:
        if isinstance(parent, str) and parent:
            children.setdefault(parent, []).append(thread_id)
    lineages = {root_thread_id: (root_thread_id,)}
    pending = [root_thread_id]
    while pending:
        parent = pending.pop()
        parent_lineage = lineages[parent]
        for child in children.get(parent, ()):
            if child in lineages:
                continue
            lineages[child] = (*parent_lineage, child)
            pending.append(child)
    return lineages


def register_host_access_descendant(
    *,
    parent_thread_id: str,
    thread_id: str,
    user_id: str,
) -> bool:
    """Register one just-committed canonical child with eligible owners."""
    if not all(
        isinstance(value, str) and value
        for value in (parent_thread_id, thread_id, user_id)
    ):
        return False
    eligible = [
        owner
        for owner in _owners.values()
        if owner.user_id == user_id
        and parent_thread_id in owner.lineages
        and supervisor_is_healthy(owner.lease_id)
    ]
    if not eligible:
        return False
    proposed = {
        owner.lease_id: (*owner.lineages[parent_thread_id], thread_id)
        for owner in eligible
    }
    if any(
        thread_id in owner.lineages
        and owner.lineages[thread_id] != proposed[owner.lease_id]
        for owner in eligible
    ):
        return False
    for owner in eligible:
        owner.lineages[thread_id] = proposed[owner.lease_id]
    return True


async def resolve_active_host_access_lease(
    thread_id: str,
) -> dict[str, Any] | None:
    """Resolve the nearest process-private live owner for one thread."""
    if not isinstance(thread_id, str) or not thread_id:
        return None
    owner = _select_owner(thread_id)
    return _owner_public(owner) if owner is not None else None


async def list_host_access_leases(
    user_id: str,
    *,
    root_thread_id: str | None = None,
    nonterminal_only: bool = False,
) -> list[dict[str, Any]]:
    """List durable rows overlaid with authoritative live owner snapshots."""
    live_rows = [
        _owner_public(owner)
        for owner in _owners.values()
        if (
            owner.user_id == user_id
            and (root_thread_id is None or owner.root_thread_id == root_thread_id)
            and _owner_is_active(owner)
        )
    ]
    try:
        rows = await run_in_db(
            lambda: list_leases(
                user_id,
                root_thread_id=root_thread_id,
                nonterminal_only=nonterminal_only,
            )
        )
    except Exception as exc:
        if not live_rows:
            raise
        from loguru import logger

        logger.warning(
            "host-access lease projection unavailable; using {} live owner "
            "snapshot(s): {}",
            len(live_rows),
            exc,
        )
        rows = []
    by_id = {
        row["id"]: row
        for row in rows
        if row.get("state") != "active"
    }
    for row in live_rows:
        by_id[row["id"]] = row
    return sorted(
        by_id.values(),
        key=lambda row: (str(row.get("created_at") or ""), str(row["id"])),
        reverse=True,
    )


async def get_host_access_lease(
    lease_id: str,
    *,
    user_id: str,
) -> dict[str, Any]:
    """Get a lease, preferring its process-private live snapshot."""
    owner = _owners.get(lease_id)
    if owner is not None and owner.user_id == user_id and _owner_is_active(owner):
        return _owner_public(owner)
    lease = await run_in_db(lambda: get_lease(lease_id, user_id=user_id))
    if lease.get("state") == "active":
        raise LeaseNotFoundError("host-access lease not found")
    return lease


def _notify_changed(lease: dict[str, Any]) -> None:
    from host_access.change_nudge import changed_message
    from infra import state

    state.broadcast_to_user(lease["user_id"], changed_message(lease))


async def _emit_terminal(lease: dict[str, Any]) -> None:
    """Notify and clean once, attempting cleanup even if notification fails."""
    failure: BaseException | None = None
    try:
        _notify_changed(lease)
    except BaseException as exc:
        failure = exc
    try:
        await _run_terminal_cleanup(lease)
    except BaseException as exc:
        if failure is None:
            failure = exc
        else:
            failure.add_note(
                "host-access terminal cleanup also failed: "
                f"{type(exc).__name__}: {exc}"
            )
    if failure is not None:
        raise failure


async def _run_terminal_cleanup(lease: dict[str, Any]) -> None:
    """Run the configured terminal cleanup without requiring durable state."""
    callback = _terminal_callback
    if callback is None:
        return
    try:
        result = callback(lease)
        if inspect.isawaitable(result):
            await result
    except BaseException as exc:  # process cleanup failure must stay loud
        from loguru import logger

        logger.error(
            "host-access terminal cleanup failed for {}: {}",
            lease["id"],
            exc,
        )
        raise


def _application_payload(lease: dict[str, Any]) -> dict[str, Any]:
    return {
        "lease_id": lease["id"],
        "application_hash": lease["application_hash"],
        "root_thread_id": lease["root_thread_id"],
        "purpose": lease["purpose"],
        "plan": lease["plan"],
        "duration_seconds": lease["requested_duration_seconds"],
        "expected_resources": lease["expected_resources"],
        "persistent_effects": lease["persistent_effects"],
        "rollback_plan": lease["rollback_plan"],
        "forbidden_effects": lease["forbidden_effects"],
        "requesting_project_id": lease["requesting_project_id"],
        "repo_policy_snapshot": lease["repo_policy_snapshot"],
        "repo_policy_hash": lease["repo_policy_hash"],
        "requested_policy_exceptions": lease[
            "requested_policy_exceptions"
        ],
    }


async def _repo_policy_is_current(lease: dict[str, Any]) -> bool:
    """Compare one stored application snapshot with current project policy."""

    def compare() -> bool:
        from infra.database import get_db
        from infra.project_repo_policy import (
            load_project_repo_policy_snapshot,
            RepoPolicyQueryError,
        )

        db = get_db()
        try:
            try:
                rows, _encoded, digest = load_project_repo_policy_snapshot(
                    lease.get("requesting_project_id"),
                    db=db,
                )
            except RepoPolicyQueryError:
                return False
            return (
                rows == lease.get("repo_policy_snapshot")
                and digest == lease.get("repo_policy_hash")
            )
        finally:
            db.close()

    return await run_in_db(compare)


def _action_target_path(
    tool_name: str,
    descriptor: dict[str, Any],
) -> Path | None:
    """Extract the resolved path carried by a canonical host-action descriptor."""
    if tool_name == "bash":
        candidate = descriptor.get("working_directory")
    else:
        path = descriptor.get("path")
        candidate = path.get("resolved") if isinstance(path, dict) else None
    if not isinstance(candidate, str) or not candidate:
        return None
    target = Path(candidate).expanduser()
    return target if target.is_absolute() else None


def _missing_policy_exception_error(
    application: dict[str, Any],
    *,
    tool_name: str,
    descriptor: dict[str, Any],
) -> str | None:
    """Return the pre-review base-checkout denial, or defer to reviewer."""
    target = _action_target_path(tool_name, descriptor)
    if target is None:
        return None
    try:
        repo = registered_repo_for_path(target)
        if repo is None or repo_worktree_root(target, repo.path) is not None:
            return None
    except Exception:
        logger.warning(
            "host-access repository containment could not be resolved for {}",
            target,
        )
        return None
    for policy, description, active in (
        ("branch_pinning", "branch-pinned", repo.branch_pinned),
        (
            "protected_base_checkout",
            "base-protected",
            repo.protect_base_checkout,
        ),
    ):
        if active and not lease_has_policy_exception(
            application,
            repo_name=repo.repo_name,
            policy=policy,
        ):
            return (
                f"Blocked: this action targets '{repo.repo_name}', which is "
                f"{description}, and the active lease has no matching policy "
                "exception. Work in a `.worktrees/` worktree instead, or submit "
                f"a new lease application with a `{policy}` exception for "
                f"'{repo.repo_name}'."
            )
    return None


async def _close_user_model_browsers(user_id: str) -> None:
    """Prove all model-owned browser sessions are closed before prompting."""

    def session_ids() -> tuple[str, ...]:
        from infra.database import get_db

        db = get_db()
        try:
            rows = db.execute(
                "SELECT id FROM threads WHERE user_id=? ORDER BY id",
                (user_id,),
            ).fetchall()
            return tuple(str(row["id"]) for row in rows)
        finally:
            db.close()

    from host_access.processes import PROCESS_OWNER

    owned = await run_in_db(session_ids)
    results = await asyncio.gather(
        *(PROCESS_OWNER.close_browser(thread_id) for thread_id in owned),
        return_exceptions=True,
    )
    failure = next(
        (result for result in results if isinstance(result, BaseException)),
        None,
    )
    if failure is not None:
        raise HostAccessBrowserIsolationError(
            "model browser isolation could not be established"
        ) from failure


def _reviewer_binary(target) -> str | None:
    if target.harness == "native":
        return None
    from connections.models import get_connection

    connection = get_connection(target.harness) or {}
    configured = (connection.get("config") or {}).get("binary")
    if configured and str(configured).strip():
        return str(configured).strip()
    return "claude" if target.harness == "claude-code" else "codex"


async def _invoke_application_review(target, payload, *, auto_approve: bool):
    from host_access.reviewer import review_application

    return await review_application(
        target,
        payload,
        auto_approve=auto_approve,
        binary=_reviewer_binary(target),
        usage_thread_id=payload.get("root_thread_id"),
    )


async def _invoke_action_review(target, application, action):
    from host_access.reviewer import review_action

    return await review_action(
        target,
        application,
        action,
        binary=_reviewer_binary(target),
        usage_thread_id=application.get("root_thread_id"),
    )


async def _reviewer_target_or_unavailable(
    reviewer_model: str | None,
    requester_thread_id: str | None,
):
    """Resolve the reviewer target, mapping failure to one denied action."""
    try:
        return await reviewer_target.resolve_reviewer_target(
            reviewer_model,
            requester_thread_id=requester_thread_id,
        )
    except asyncio.CancelledError:
        raise
    except Exception as exc:
        raise ActionReviewUnavailableError(
            f"host-access action reviewer is unavailable: {failure_detail(exc)}"
        ) from exc



async def _action_review_or_unavailable(target, application, action):
    """Obtain one fresh verdict, mapping failure to one denied action."""
    try:
        return await _invoke_action_review(target, application, action)
    except asyncio.CancelledError:
        raise
    except Exception as exc:
        raise ActionReviewUnavailableError(
            f"host-access action review did not complete: {failure_detail(exc)}"
        ) from exc


async def _deliver_activation_wake(lease: dict[str, Any]) -> bool:
    from events.delivery import deliver_to_thread

    return await deliver_to_thread(
        lease["root_thread_id"],
        "host_access.activated",
        "Reviewed temporary host access is active.",
        {"lease_id": lease["id"]},
    )


# One best-effort courtesy retry of the activation wake. The requesting thread
# is usually still mid-turn when its lease activates (thread-input delivery is
# rejected with "turn already active"), so the retry waits long enough for a
# short turn to end. Wake delivery never gates lease liveness.
_ACTIVATION_WAKE_RETRY_DELAY_SECONDS = 30.0


def _schedule_activation_wake_retry(owner: _LeaseOwner) -> None:
    """Schedule the single bounded retry of a failed activation wake."""
    if owner.wake_retry_task is not None:
        return
    owner.wake_retry_task = asyncio.create_task(
        _retry_activation_wake(owner),
        name=f"host-access-wake-retry:{owner.lease_id}",
    )


async def _retry_activation_wake(owner: _LeaseOwner) -> None:
    """Retry the activation wake once, then give up on notifying.

    The wake is a courtesy notification: its failure never changes lease
    state. ``activation_wake_delivered_at`` stays NULL — the truthful durable
    record of non-delivery — unless a wake actually reaches the thread, so a
    receipt is never fabricated. Errors here must never propagate.
    """
    try:
        await asyncio.sleep(_ACTIVATION_WAKE_RETRY_DELAY_SECONDS)
        revision = owner.active_revision
        if (
            _owners.get(owner.lease_id) is not owner
            or not _owner_is_active(owner)
            or revision is None
        ):
            return
        if not await _deliver_activation_wake(_owner_public(owner)):
            logger.error(
                "host-access activation wake for lease {} failed again on its "
                "single retry (delivery failure reason on the preceding "
                "event-routing error line); giving up on the notification — "
                "the lease remains active and armed, and the holder discovers "
                "it by polling or on its next action",
                owner.lease_id,
            )
            return
        try:
            current = await run_in_db(
                lambda: record_activation_wake_delivery(
                    owner.lease_id,
                    expected_revision=revision,
                    application_hash=owner.application_hash,
                )
            )
        except (LeaseConflictError, LeaseNotFoundError) as exc:
            logger.warning(
                "host-access activation wake retry for lease {} was delivered "
                "but the lease changed concurrently ({}); the wake receipt "
                "stays absent",
                owner.lease_id,
                exc,
            )
            return
        if _owners.get(owner.lease_id) is owner:
            owner.public_lease_json = _encoded(current)
        _notify_changed(current)
        logger.info(
            "host-access activation wake for lease {} was delivered on retry",
            owner.lease_id,
        )
    except asyncio.CancelledError:
        raise
    except Exception:
        logger.exception(
            "host-access activation wake retry for lease {} failed "
            "unexpectedly; the lease is unaffected",
            owner.lease_id,
        )


async def _current(lease_id: str) -> dict[str, Any]:
    return await run_in_db(lambda: get_lease(lease_id))


async def _safe_block(
    lease_id: str,
    reason: str,
    *,
    owner_hint: _LeaseOwner | None = None,
) -> dict[str, Any]:
    owner = _owners.get(lease_id) or owner_hint
    if owner is not None:
        return await _terminalize_owner(
            owner,
            state="blocked",
            reason=reason,
        )
    row = await _current(lease_id)
    won_transition = False
    if row["state"] in NONTERMINAL_STATES:
        try:
            row = await run_in_db(
                lambda: block_lease(
                    lease_id,
                    expected_revision=row["revision"],
                    application_hash=row["application_hash"],
                    reason=reason,
                )
            )
            won_transition = True
        except LeaseConflictError:
            row = await _current(lease_id)
    if won_transition:
        await _emit_terminal(row)
    else:
        _notify_changed(row)
    return row


def _terminal_owner_snapshot(
    owner: _LeaseOwner,
    *,
    state: str,
    reason: str,
) -> dict[str, Any]:
    row = _owner_public(owner)
    row["state"] = state
    row["terminal_reason"] = reason
    row["revision"] = int(row.get("revision") or 0) + 1
    row["updated_at"] = utc_now_iso()
    return row


async def _project_owner_terminal(
    owner: _LeaseOwner,
    *,
    state: str,
    reason: str,
) -> dict[str, Any]:
    """Best-effort durable projection after live authority is fenced."""
    fallback = _terminal_owner_snapshot(owner, state=state, reason=reason)
    revision = owner.active_revision
    if revision is None:
        revision = int(_owner_public(owner).get("revision") or 0)
    try:
        return await run_in_db(
            lambda: terminate_lease_projection(
                owner.lease_id,
                expected_revision=revision,
                application_hash=owner.application_hash,
                target_state=state,
                reason=reason,
            )
        )
    except Exception as exc:
        from loguru import logger

        logger.warning(
            "host-access terminal projection failed for {}: {}",
            owner.lease_id,
            exc,
        )
        return fallback


async def _finish_owner_terminalization(
    owner: _LeaseOwner,
    *,
    state: str,
    reason: str,
    before_projection: Callable[[], Awaitable[None]] | None = None,
    preparation_failure_reason: str = "terminal_preparation_failed",
) -> dict[str, Any]:
    """Project and clean exactly once after the owner has been fenced."""
    try:
        if before_projection is not None:
            try:
                await before_projection()
            except BaseException as exc:
                from loguru import logger

                logger.error(
                    "host-access terminal preparation failed for {}: {}",
                    owner.lease_id,
                    exc,
                )
                state = "blocked"
                reason = preparation_failure_reason
        row = await _project_owner_terminal(
            owner,
            state=state,
            reason=reason,
        )
        await _emit_terminal(row)
        return row
    finally:
        if _owners.get(owner.lease_id) is owner:
            _owners.pop(owner.lease_id, None)


def _claim_owner_terminalization(
    owner: _LeaseOwner,
    *,
    state: str,
    reason: str,
    before_projection: Callable[[], Awaitable[None]] | None = None,
    preparation_failure_reason: str = "terminal_preparation_failed",
) -> asyncio.Task[dict[str, Any]]:
    """Synchronously fence one owner and claim its single terminal task."""
    terminal_task = owner.terminal_task
    if terminal_task is None:
        owner.graceful = True
        owner.stopping = True
        owner.permits.clear()
        _cancel_wake_retry(owner)
        owner_task = owner.task
        current_task = asyncio.current_task()
        if (
            owner_task is not None
            and owner_task is not current_task
            and not owner_task.done()
        ):
            owner_task.cancel()
        terminal_task = asyncio.create_task(
            _finish_owner_terminalization(
                owner,
                state=state,
                reason=reason,
                before_projection=before_projection,
                preparation_failure_reason=preparation_failure_reason,
            ),
            name=f"host-access-terminal:{owner.lease_id}",
        )
        owner.terminal_task = terminal_task
    return terminal_task


async def _terminalize_owner(
    owner: _LeaseOwner,
    *,
    state: str,
    reason: str,
    before_projection: Callable[[], Awaitable[None]] | None = None,
    preparation_failure_reason: str = "terminal_preparation_failed",
) -> dict[str, Any]:
    """Join one owner's first terminal intent through cleanup."""
    terminal_task = _claim_owner_terminalization(
        owner,
        state=state,
        reason=reason,
        before_projection=before_projection,
        preparation_failure_reason=preparation_failure_reason,
    )
    return await asyncio.shield(terminal_task)


async def _run_owner(owner: _LeaseOwner) -> None:
    if not owner.ready.done():
        owner.ready.set_result(None)
    try:
        await owner.activated.wait()
        deadline = owner.monotonic_deadline
        if deadline is None:
            raise HostAccessSupervisorError(
                "host-access owner deadline is unavailable"
            )
        delay = deadline - time.monotonic()
        if delay > 0:
            await asyncio.sleep(delay)
        await _terminalize_owner(
            owner,
            state="expired",
            reason="lease_expired",
        )
    except asyncio.CancelledError:
        if not owner.graceful:
            await _terminalize_owner(
                owner,
                state="blocked",
                reason="host_access_supervisor_stopped",
            )
    except Exception:
        if owner.terminal_task is not None:
            raise
        if not owner.graceful:
            await _terminalize_owner(
                owner,
                state="blocked",
                reason="host_access_supervisor_failure",
            )
    finally:
        if owner.terminal_task is None and _owners.get(owner.lease_id) is owner:
            _owners.pop(owner.lease_id, None)


async def _install_owner(
    lease: dict[str, Any],
    *,
    approval_revision: int,
) -> _LeaseOwner:
    existing = _owners.get(lease["id"])
    if (
        existing is not None
        and supervisor_is_healthy(lease["id"])
        and existing.application_hash == lease["application_hash"]
        and existing.reviewer_model == lease["reviewer_model"]
        and existing.reviewer_binding == reviewer_target.reviewer_binding(lease)
        and existing.root_thread_id == lease["root_thread_id"]
        and existing.user_id == lease["user_id"]
        and existing.approval_revision == approval_revision
    ):
        return existing
    if existing is not None:
        await _stop_owner(lease["id"], graceful=True)
    loop = asyncio.get_running_loop()
    owner = _LeaseOwner(
        lease_id=lease["id"],
        application_hash=lease["application_hash"],
        reviewer_model=lease["reviewer_model"],
        reviewer_binding=reviewer_target.reviewer_binding(lease),
        ready=loop.create_future(),
        root_thread_id=lease["root_thread_id"],
        user_id=lease["user_id"],
        application_json=_encoded(_application_payload(lease)),
        public_lease_json=_encoded(lease),
        approval_revision=approval_revision,
        lineages={lease["root_thread_id"]: (lease["root_thread_id"],)},
    )
    _owners[lease["id"]] = owner
    owner.task = asyncio.create_task(
        _run_owner(owner),
        name=f"host-access:{lease['id']}",
    )
    await owner.ready
    await asyncio.sleep(0)
    if owner.task.done():
        raise HostAccessSupervisorError("host-access supervisor failed to start")
    try:
        owner.lineages.update(
            await _snapshot_canonical_lineages(
                root_thread_id=owner.root_thread_id,
                user_id=owner.user_id,
            )
        )
    except BaseException:
        await _stop_owner(owner.lease_id, graceful=True)
        raise
    return owner


def _arm_owner(owner: _LeaseOwner, lease: dict[str, Any]) -> None:
    """Freeze active revision and dual deadlines before granting authority."""
    if (
        lease.get("state") != "active"
        or lease.get("id") != owner.lease_id
        or lease.get("root_thread_id") != owner.root_thread_id
        or lease.get("user_id") != owner.user_id
        or lease.get("application_hash") != owner.application_hash
        or lease.get("reviewer_model") != owner.reviewer_model
        or reviewer_target.reviewer_binding(lease) != owner.reviewer_binding
    ):
        raise HostAccessSupervisorError(
            "host-access activation identity is invalid"
        )
    revision = lease.get("revision")
    expires_raw = lease.get("expires_at")
    if (
        isinstance(revision, bool)
        or not isinstance(revision, int)
        or not isinstance(expires_raw, str)
    ):
        raise HostAccessSupervisorError(
            "host-access activation deadline is invalid"
        )
    expires_at = _as_utc(expires_raw)
    if owner.activated.is_set():
        if owner.active_revision != revision or owner.expires_at != expires_at:
            raise HostAccessSupervisorError(
                "host-access owner is already armed with different authority"
            )
        return
    remaining = (expires_at - datetime.now(timezone.utc)).total_seconds()
    if remaining <= 0:
        raise HostAccessSupervisorError("host-access lease already expired")
    owner.active_revision = revision
    owner.expires_at = expires_at
    owner.monotonic_deadline = time.monotonic() + remaining
    owner.public_lease_json = _encoded(lease)
    owner.activated.set()


def _cancel_wake_retry(owner: _LeaseOwner) -> None:
    """Drop a pending courtesy wake retry when its owner is fenced."""
    retry_task = owner.wake_retry_task
    if retry_task is not None and not retry_task.done():
        retry_task.cancel()


async def _stop_owner(lease_id: str, *, graceful: bool) -> None:
    owner = _owners.get(lease_id)
    if owner is None or owner.task is None:
        return
    owner.graceful = graceful
    owner.stopping = True
    owner.permits.clear()
    _cancel_wake_retry(owner)
    if _owners.get(lease_id) is owner:
        _owners.pop(lease_id, None)
    if not owner.task.done():
        owner.task.cancel()
    if owner.task is not asyncio.current_task():
        await asyncio.gather(owner.task, return_exceptions=True)


async def request_reviewed_lease(
    thread_id: str,
    **application: Any,
) -> dict[str, Any]:
    """Commit one application, then run exactly one independent review."""
    lease = await run_in_db(
        lambda: request_lease(thread_id, **application)
    )
    lock = _application_locks.setdefault(lease["id"], asyncio.Lock())
    try:
        async with lock:
            lease = await _current(lease["id"])
            if lease["state"] != "reviewing":
                return lease
            review_recorded = False
            try:
                review_auto_approve = (
                    await run_in_db(get_host_access_auto_approve)
                    and not lease["requested_policy_exceptions"]
                )
                target = await reviewer_target.resolve_reviewer_target(requester_thread_id=thread_id)
                review = await _invoke_application_review(
                    target,
                    _application_payload(lease),
                    auto_approve=review_auto_approve,
                )
                if reviewer_target.identity_binding(review.identity) != reviewer_target.target_binding(target):
                    raise HostAccessSupervisorError(
                        "host-access reviewer identity changed"
                    )
                isolation_failed = False
                if review.allowed:
                    try:
                        await _close_user_model_browsers(lease["user_id"])
                    except HostAccessBrowserIsolationError:
                        isolation_failed = True
                # Keep the durable lease non-approvable until isolation finishes.
                # A failed isolation records the verdict and block atomically.
                if not await _repo_policy_is_current(lease):
                    return await _safe_block(
                        lease["id"],
                        "project_policy_changed",
                    )
                auto_approve = (
                    review_auto_approve
                    and await run_in_db(get_host_access_auto_approve)
                )
                lease = await run_in_db(
                    lambda: record_application_review(
                        lease["id"],
                        expected_revision=lease["revision"],
                        application_hash=lease["application_hash"],
                        allowed=review.allowed,
                        auto_approve=auto_approve,
                        isolation_failed=isolation_failed,
                        reviewer_model=review.identity.catalog_id,
                        review_result=application_review_record(review),
                    )
                )
                review_recorded = True
                if isolation_failed:
                    await _emit_terminal(lease)
                    return lease
                _notify_changed(lease)
                if lease["state"] == "activating":
                    return await approve_and_activate(
                        lease["id"],
                        user_id=lease["user_id"],
                        expected_revision=lease["revision"],
                        application_hash=lease["application_hash"],
                        reviewer_approved=True,
                    )
                return lease
            except asyncio.CancelledError:
                await _safe_block(
                    lease["id"],
                    (
                        "activation_interrupted"
                        if review_recorded
                        else "application_review_interrupted"
                    ),
                )
                raise
            except Exception as exc:
                # A sanitized chain, not a traceback: frame locals can hold secrets.
                logger.warning(
                    "host-access application flow failed for lease {}: {}",
                    lease["id"], exception_chain_summary(exc),
                )
                reason = lease_failure_reason(exc, review_recorded=review_recorded)
                return reviewer_target.with_failure_cause(await _safe_block(lease["id"], reason), exc)
    finally:
        if _application_locks.get(lease["id"]) is lock:
            _application_locks.pop(lease["id"], None)


async def approve_and_activate(
    lease_id: str,
    *,
    user_id: str,
    expected_revision: int,
    application_hash: str,
    reviewer_approved: bool = False,
) -> dict[str, Any]:
    """Apply a human or recorded reviewer approval, then activate safely."""
    lock = _activation_locks.setdefault(lease_id, asyncio.Lock())
    try:
        async with lock:
            owner = _owners.get(lease_id)
            if owner is not None:
                if owner.user_id != user_id:
                    raise LeaseNotFoundError("host-access lease not found")
                if owner.application_hash != application_hash:
                    raise LeaseConflictError("application hash does not match")
                if expected_revision not in {
                    owner.approval_revision,
                    owner.active_revision,
                }:
                    raise LeaseConflictError("lease revision is stale")
                if owner.terminal_task is not None:
                    return await asyncio.shield(owner.terminal_task)
                if _owner_is_active(owner):
                    return _owner_public(owner)
            current = await _current(lease_id)
            if not await _repo_policy_is_current(current):
                return await _safe_block(
                    lease_id,
                    "project_policy_changed",
                )
            if reviewer_approved:
                review_result = current.get("application_review_result")
                if (
                    current["state"] != "activating"
                    or current["user_id"] != user_id
                    or current["revision"] != expected_revision
                    or current["application_hash"] != application_hash
                    or current["user_decision"] is not None
                    or not isinstance(review_result, dict)
                    or review_result.get("approval_mode") != "automatic"
                ):
                    raise LeaseConflictError(
                        "automatic reviewer approval is not current"
                    )
                approved = current
            else:
                approved = await run_in_db(
                    lambda: approve_lease(
                        lease_id,
                        user_id=user_id,
                        expected_revision=expected_revision,
                        application_hash=application_hash,
                    )
                )
            if approved["state"] not in {"activating", "active"}:
                return approved
            if approved["state"] == "active":
                if (
                    approved["activation_wake_delivered_at"] is None
                    or not supervisor_is_healthy(lease_id)
                ):
                    return await _safe_block(
                        lease_id,
                        "activation_owner_unavailable",
                    )
                owner = _owners.get(lease_id)
                if (
                    owner is None
                    or owner.application_hash != approved["application_hash"]
                    or owner.reviewer_binding != reviewer_target.reviewer_binding(approved)
                    or not _owner_is_active(owner)
                ):
                    return await _safe_block(
                        lease_id,
                        "activation_owner_identity_failed",
                    )
                return approved
            try:
                binding = reviewer_target.reviewer_binding(approved)
                target = await reviewer_target.resolve_bound_reviewer_target(
                    approved["reviewer_model"],
                    binding,
                    approved["root_thread_id"],
                    resolver=reviewer_target.resolve_reviewer_target,
                )
                owner = await _install_owner(
                    approved,
                    approval_revision=expected_revision,
                )
                if not await _repo_policy_is_current(
                    _owner_application(owner)
                ):
                    return await _safe_block(
                        lease_id,
                        "project_policy_changed",
                    )
                active = await run_in_db(
                    lambda: activate_lease(
                        lease_id,
                        expected_revision=approved["revision"],
                        application_hash=approved["application_hash"],
                    )
                )
                if active["state"] != "active":
                    return await _safe_block(
                        lease_id,
                        "activation_identity_failed",
                    )
                try:
                    _arm_owner(owner, active)
                except HostAccessSupervisorError:
                    return await _safe_block(
                        lease_id,
                        "activation_identity_failed",
                    )
                _notify_changed(active)
                if reviewer_approved:
                    # The tool result is the in-band activation signal. Sending
                    # a wake while its request turn is active always fails and
                    # would schedule a redundant turn after the agent proceeds.
                    return active
                if not await _deliver_activation_wake(active):
                    # The wake is a courtesy notification; its failure is not
                    # a lease fault. The armed lease stays active, the missing
                    # receipt (activation_wake_delivered_at NULL) durably
                    # records the non-delivery, and one bounded retry runs in
                    # the background.
                    logger.error(
                        "host-access activation wake for lease {} (root "
                        "session {}) was not delivered (delivery failure "
                        "reason on the preceding event-routing error line); "
                        "the lease stays active and armed; retrying the wake "
                        "once in {}s",
                        lease_id,
                        active["root_thread_id"],
                        _ACTIVATION_WAKE_RETRY_DELAY_SECONDS,
                    )
                    _schedule_activation_wake_retry(owner)
                    return active
                current = await run_in_db(
                    lambda: record_activation_wake_delivery(
                        lease_id,
                        expected_revision=active["revision"],
                        application_hash=active["application_hash"],
                    )
                )
                if current["state"] != "active":
                    return await _terminalize_owner(
                        owner,
                        state="blocked",
                        reason="activation_owner_unavailable",
                    )
                owner.public_lease_json = _encoded(current)
                return current
            except asyncio.CancelledError:
                await _safe_block(lease_id, "activation_interrupted")
                raise
            except Exception:
                return await _safe_block(lease_id, "activation_failed")
    finally:
        if _activation_locks.get(lease_id) is lock:
            _activation_locks.pop(lease_id, None)


async def release_requested_lease(thread_id: str) -> dict[str, Any] | None:
    """End the requesting thread's own lease early; return it, or ``None``.

    Releasing gives up authority, so it needs no user decision. It is the
    supported way for a holder whose scope has changed to end the current
    lease and apply for a new, differently scoped one.
    """
    lease = await run_in_db(lambda: get_open_lease_for_session(thread_id))
    if lease is None:
        return None
    owner = _owners.get(lease["id"])
    if owner is not None and owner.root_thread_id == thread_id:
        return await _terminalize_owner(
            owner,
            state="revoked",
            reason="agent_released",
        )
    released = await run_in_db(
        lambda: release_lease(
            lease["id"],
            root_thread_id=thread_id,
            expected_revision=lease["revision"],
            application_hash=lease["application_hash"],
        )
    )
    await _emit_terminal(released)
    return released


async def reject_pending_lease(
    lease_id: str,
    *,
    user_id: str,
    expected_revision: int,
    application_hash: str,
) -> dict[str, Any]:
    lease = await run_in_db(
        lambda: reject_lease(
            lease_id,
            user_id=user_id,
            expected_revision=expected_revision,
            application_hash=application_hash,
        )
    )
    await _emit_terminal(lease)
    return lease


async def revoke_active_lease(
    lease_id: str,
    *,
    user_id: str,
    expected_revision: int,
    application_hash: str,
) -> dict[str, Any]:
    owner = _owners.get(lease_id)
    if owner is not None:
        if owner.user_id != user_id:
            raise LeaseNotFoundError("host-access lease not found")
        if owner.application_hash != application_hash:
            raise LeaseConflictError("application hash does not match")
        if owner.active_revision != expected_revision:
            raise LeaseConflictError("lease revision is stale")
        return await _terminalize_owner(
            owner,
            state="revoked",
            reason="user_revoked",
        )
    else:
        lease = await run_in_db(
            lambda: revoke_lease(
                lease_id,
                user_id=user_id,
                expected_revision=expected_revision,
                application_hash=application_hash,
            )
        )
    await _emit_terminal(lease)
    return lease


async def review_host_action(
    lease_id: str,
    *,
    thread_id: str,
    tool_name: str,
    action_hash: str,
    redacted_descriptor: dict[str, Any],
) -> dict[str, Any]:
    """Serialize and durably bind one fresh action verdict."""
    owner = _owners.get(lease_id)
    if owner is None or not _owner_is_active(owner):
        await _safe_block(lease_id, "host_access_supervisor_unavailable")
        raise HostAccessSupervisorError("host-access supervisor is unavailable")
    async with owner.review_lock:
        try:
            validate_action_descriptor(tool_name, redacted_descriptor)
            lineage = owner.lineages.get(thread_id)
            if (
                _owners.get(lease_id) is not owner
                or not _owner_is_active(owner)
                or lineage is None
            ):
                raise HostAccessSupervisorError(
                    "host-access supervisor identity is stale"
                )
            application = _owner_application(owner)
            if not await _repo_policy_is_current(application):
                await _safe_block(lease_id, "project_policy_changed")
                raise ProjectPolicyChangedError(
                    "project policy changed; request host access again"
                )
            if denial := _missing_policy_exception_error(
                application,
                tool_name=tool_name,
                descriptor=redacted_descriptor,
            ):
                raise PolicyExceptionScreenError(denial)
            target = await _reviewer_target_or_unavailable(
                owner.reviewer_model, owner.root_thread_id
            )
            reviewer_target.require_reviewer_binding(target, owner.reviewer_binding)
            if (
                _owners.get(lease_id) is not owner
                or not _owner_is_active(owner)
                or owner.lineages.get(thread_id) != lineage
            ):
                raise HostAccessSupervisorError(
                    "host-access supervisor identity is stale"
                )
            assert owner.monotonic_deadline is not None
            remaining = max(0, int(owner.monotonic_deadline - time.monotonic()))
            action = {
                "lease_id": lease_id,
                "lease_revision": owner.active_revision,
                "session_id": thread_id,
                "lineage": list(lineage),
                "remaining_seconds": remaining,
                "tool_name": tool_name,
                "action_hash": action_hash,
                "descriptor": redacted_descriptor,
            }
            review = await _action_review_or_unavailable(
                target,
                application,
                action,
            )
            current_target = await _reviewer_target_or_unavailable(
                owner.reviewer_model, owner.root_thread_id
            )
            if (
                reviewer_target.target_binding(current_target) != owner.reviewer_binding
                or reviewer_target.identity_binding(review.identity) != owner.reviewer_binding
                or _owners.get(lease_id) is not owner
                or not _owner_is_active(owner)
                or owner.lineages.get(thread_id) != lineage
            ):
                raise HostAccessSupervisorError(
                    "host-access reviewer identity is stale"
                )
            if not await _repo_policy_is_current(application):
                await _safe_block(lease_id, "project_policy_changed")
                raise ProjectPolicyChangedError(
                    "project policy changed; request host access again"
                )
            assert owner.active_revision is not None
            permit_monotonic_deadline = None
            permit_expires_at = None
            if review.verdict == "allow":
                assert owner.monotonic_deadline is not None
                assert owner.expires_at is not None
                permit_monotonic_deadline = min(
                    owner.monotonic_deadline,
                    time.monotonic() + ACTION_PERMIT_TTL_SECONDS,
                )
                permit_expires_at = min(
                    owner.expires_at,
                    datetime.now(timezone.utc)
                    + timedelta(seconds=ACTION_PERMIT_TTL_SECONDS),
                ).isoformat()
            review_row: dict[str, Any] | None = None

            async def append_review_audit() -> None:
                nonlocal review_row
                review_row = await run_in_db(
                    lambda: record_action_review(
                        lease_id,
                        thread_id=thread_id,
                        lease_revision=owner.active_revision,
                        tool_name=tool_name,
                        action_hash=action_hash,
                        redacted_descriptor=redacted_descriptor,
                        reviewer_model=review.identity.catalog_id,
                        verdict=review.verdict,
                        reason=review.reason,
                        permit_expires_at=permit_expires_at,
                    )
                )

            if review.verdict == "revoke":
                lease = await _terminalize_owner(
                    owner,
                    state="revoked",
                    reason="action_review_revoked",
                    before_projection=append_review_audit,
                    preparation_failure_reason="action_reviewer_failure",
                )
                if review_row is None:
                    raise HostAccessSupervisorError(
                        "host-access revoke audit failed"
                    )
            else:
                await append_review_audit()
                if (
                    _owners.get(lease_id) is not owner
                    or not _owner_is_active(owner)
                    or owner.lineages.get(thread_id) != lineage
                ):
                    raise HostAccessSupervisorError(
                        "host-access supervisor changed before permit handoff"
                    )
                if not await _repo_policy_is_current(application):
                    await _safe_block(lease_id, "project_policy_changed")
                    raise ProjectPolicyChangedError(
                        "project policy changed; request host access again"
                    )
                if (
                    _owners.get(lease_id) is not owner
                    or not _owner_is_active(owner)
                    or owner.lineages.get(thread_id) != lineage
                ):
                    raise HostAccessSupervisorError(
                        "host-access supervisor changed before permit handoff"
                    )
                if review.verdict == "allow":
                    permit_id = review_row.get("permit_id")
                    if not isinstance(permit_id, str) or not permit_id:
                        raise HostAccessSupervisorError(
                            "host-access permit audit is invalid"
                        )
                    assert permit_monotonic_deadline is not None
                    owner.permits[permit_id] = _OwnerPermit(
                        permit_id=permit_id,
                        thread_id=thread_id,
                        lease_revision=owner.active_revision,
                        application_hash=owner.application_hash,
                        tool_name=tool_name,
                        action_hash=action_hash,
                        monotonic_deadline=permit_monotonic_deadline,
                    )
                lease = _owner_public(owner)
            assert review_row is not None
            result = {"review": review_row, "lease": lease}
        except asyncio.CancelledError:
            await _safe_block(
                lease_id,
                "action_review_interrupted",
                owner_hint=owner,
            )
            raise
        except ProjectPolicyChangedError:
            raise
        except PolicyExceptionScreenError:
            raise
        except ActionReviewUnavailableError as exc:
            # The reviewer failed before a verdict: deny this one action and
            # keep the armed lease live so the next attempt re-reviews. No
            # audit row exists for a verdict-less failure, so log the reason.
            logger.warning(
                "host-access action review unavailable for lease {}; "
                "action denied, lease stays armed: {}",
                lease_id,
                exc,
            )
            raise
        except Exception as exc:
            await _safe_block(
                lease_id,
                "action_reviewer_failure",
                owner_hint=owner,
            )
            raise HostAccessSupervisorError(
                "host-access action review failed closed: "
                + failure_detail(exc)
            ) from exc
    return result


async def consume_host_action_permit(
    permit_id: str,
    *,
    lease_id: str,
    thread_id: str,
    expected_revision: int,
    application_hash: str,
    tool_name: str,
    action_hash: str,
) -> None:
    """Consume one permit only while its exact lease owner remains healthy."""
    owner = _owners.get(lease_id)
    if (
        owner is None
        or owner.application_hash != application_hash
        or not _owner_is_active(owner)
    ):
        raise HostAccessSupervisorError("host-access supervisor is unavailable")
    permit = owner.permits.get(permit_id)
    if (
        permit is None
        or permit.thread_id != thread_id
        or permit.lease_revision != expected_revision
        or permit.application_hash != application_hash
        or permit.tool_name != tool_name
        or permit.action_hash != action_hash
        or time.monotonic() >= permit.monotonic_deadline
    ):
        raise HostAccessSupervisorError("host-access action permit is unavailable")
    owner.permits.pop(permit_id, None)
    application = _owner_application(owner)
    if not await _repo_policy_is_current(application):
        await _terminalize_owner(
            owner,
            state="blocked",
            reason="project_policy_changed",
        )
        raise ProjectPolicyChangedError(
            "project policy changed; request host access again"
        )
    if _owners.get(lease_id) is not owner or not _owner_is_active(owner):
        raise HostAccessSupervisorError("host-access supervisor is unavailable")
    try:
        marked = await run_in_db(
            lambda: mark_action_permit_consumed(permit_id)
        )
        if not marked:
            from loguru import logger

            logger.warning(
                "host-access permit consumption audit row missing for {}",
                lease_id,
            )
    except Exception as exc:
        from loguru import logger

        logger.warning(
            "host-access permit consumption audit failed for {}: {}",
            lease_id,
            exc,
        )
    if _owners.get(lease_id) is not owner or not _owner_is_active(owner):
        raise HostAccessSupervisorError("host-access supervisor is unavailable")


async def recover_host_access_supervisors() -> list[str]:
    """Clean and invalidate every persisted lease before serving traffic."""
    blocked_ids: list[str] = []
    live_owners = tuple(_owners.values())
    live_owner_ids = {owner.lease_id for owner in live_owners}
    for owner in live_owners:
        blocked = await _terminalize_owner(
            owner,
            state="blocked",
            reason="backend_restart_invalidated",
        )
        blocked_ids.append(blocked["id"])
    rows = await run_in_db(list_persisted_nonterminal_leases)
    for lease in rows:
        if lease["id"] in live_owner_ids:
            continue
        cleanup_failure: BaseException | None = None
        try:
            await _run_terminal_cleanup(lease)
        except BaseException as exc:
            cleanup_failure = exc
        try:
            blocked = await run_in_db(
                lambda lease=lease: terminate_lease_projection(
                    lease["id"],
                    expected_revision=lease["revision"],
                    application_hash=lease["application_hash"],
                    target_state="blocked",
                    reason="backend_restart_invalidated",
                )
            )
        except BaseException as projection_failure:
            if cleanup_failure is not None:
                projection_failure.add_note(
                    "host-access startup cleanup also failed: "
                    f"{type(cleanup_failure).__name__}: {cleanup_failure}"
                )
            raise
        _notify_changed(blocked)
        blocked_ids.append(blocked["id"])
        if cleanup_failure is not None:
            raise cleanup_failure
    return blocked_ids


async def shutdown_host_access_supervisors() -> None:
    """Fence every live owner, then project terminal shutdown state."""
    terminal_tasks = [
        _claim_owner_terminalization(
            owner,
            state="blocked",
            reason="backend_shutdown",
        )
        for owner in tuple(_owners.values())
    ]
    results = await asyncio.gather(
        *(asyncio.shield(task) for task in terminal_tasks),
        return_exceptions=True,
    )
    failures = [result for result in results if isinstance(result, BaseException)]
    if failures:
        failure = failures[0]
        for extra in failures[1:]:
            failure.add_note(
                "another host-access terminal task also failed: "
                f"{type(extra).__name__}: {extra}"
            )
        raise failure
