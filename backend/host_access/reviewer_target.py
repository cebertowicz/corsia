"""Which model reviews a host-access lease.

A pinned reviewer (the model that reviewed the application) wins; otherwise
the configured reviewer, which the requester's own model replaces for one
application review when it cannot run and the person turned fallback on;
otherwise the requesting thread's own model. The requester's model supplies
only the model identity: every review still runs as one fresh isolated call
over the application payload, never a fork of the requester's transcript.

Every candidate resolves under the requesting thread's locality policy, so a
lease's purpose and command are never disclosed to a model the person's
project or chat setting has ruled out: a refused reviewer is an unavailable
one, and a fallback candidate has to pass the same policy.
"""

from __future__ import annotations

from dataclasses import dataclass, fields, replace

from host_access.errors import (
    HostAccessSupervisorError,
    ReviewerOutage,
    ReviewerUnavailableError,
)
from infra.db_async import run_in_db
from infra.product_events import emit_product_event
from llm.resolution import ExecutionTarget


# Key on the lease returned to the requesting tool only (never persisted): the
# reviewer outage that blocked it, so its guidance reports that cause and not
# whatever the outage is by the time the guidance is built.
REVIEWER_UNAVAILABLE_ERROR_KEY = "reviewer_unavailable_error"


def with_failure_cause(blocked: dict, exc: BaseException) -> dict:
    """The blocked lease, carrying the reviewer outage that caused the block when there was one."""
    if isinstance(exc, ReviewerUnavailableError):
        return {**blocked, REVIEWER_UNAVAILABLE_ERROR_KEY: exc}
    return blocked


@dataclass(frozen=True, slots=True)
class ReviewerBinding:
    """The immutable reviewer identity recorded with an approved lease."""

    catalog_id: str
    harness: str
    transport_model: str
    reported_model: str


def reviewer_binding(lease: dict) -> ReviewerBinding:
    """Read the immutable reviewer identity from an approved lease."""
    review = lease.get("application_review_result")
    if not isinstance(review, dict):
        raise HostAccessSupervisorError("host-access application reviewer identity is unavailable")
    values = {
        "catalog_id": lease.get("reviewer_model"),
        "harness": review.get("reviewer_harness"),
        "transport_model": review.get("reviewer_transport_model"),
        "reported_model": review.get("reviewer_reported_model"),
    }
    if any(not isinstance(value, str) or not value.strip() for value in values.values()):
        raise HostAccessSupervisorError("host-access application reviewer identity is incomplete")
    return ReviewerBinding(**{key: str(value).strip() for key, value in values.items()})


def target_binding(target) -> ReviewerBinding:
    """Normalize a resolved target to its immutable reviewer identity."""
    return ReviewerBinding(
        catalog_id=target.catalog_id,
        harness=target.harness,
        transport_model=target.transport_model,
        reported_model=getattr(target, "expected_reported_model", None)
        or target.transport_model,
    )


def identity_binding(identity) -> ReviewerBinding:
    """Normalize a completed review identity to its immutable binding."""
    return ReviewerBinding(
        catalog_id=identity.catalog_id,
        harness=identity.harness,
        transport_model=identity.transport_model,
        reported_model=identity.reported_model,
    )


def require_reviewer_binding(target, expected: ReviewerBinding) -> None:
    """Reject a catalog target that was remapped after application review."""
    if target_binding(target) != expected:
        raise HostAccessSupervisorError("host-access reviewer catalog identity was remapped")


def reviewer_invocation_binding(target) -> dict[str, str | bool | None]:
    """Bind one reviewer invocation to its resolved account, when applicable."""
    return {
        "account_id": target.account_id,
        "account_bound": target.account_id is not None,
    }


def _requester_model(db, requester_thread_id: str | None) -> str | None:
    """Return the requesting thread's own selected model, if it has one."""
    if not requester_thread_id:
        return None
    row = db.execute(
        "SELECT model FROM threads WHERE id=?", (requester_thread_id,),
    ).fetchone()
    return (str(row["model"] or "").strip() or None) if row else None


def _requester_reviewer_account(db, target, requester_thread_id: str | None) -> str | None:
    """Pick the requester's project-policy account for a harness reviewer.

    The reviewer is a non-thread consumer bound to the requester's project
    policy, rather than the requester's moved account.
    """
    if target.harness not in {"claude-code", "codex"}:
        return None
    if not requester_thread_id:
        raise HostAccessSupervisorError(
            "host-access reviewer account unavailable: requesting_thread_missing"
        )

    from connections.account_policy import thread_scope
    from reviewer.failures import CCUnavailableError
    from reviewer.reviewer_account import resolve_scope_reviewer_account

    project = thread_scope(requester_thread_id, db=db)["project_id"]
    try:
        return resolve_scope_reviewer_account(target, project, db=db)
    except CCUnavailableError as exc:
        reason = getattr(exc, "reason", "unavailable")
        raise ReviewerUnavailableError(
            f"host-access reviewer account unavailable: {reason}",
            outage=ReviewerOutage(
                _model_name(target), reason, getattr(exc, "resets_at", None),
            ),
        ) from exc


def _model_name(target) -> str:
    """The name a person knows this reviewer model by."""
    return target.display_name or target.catalog_id


@dataclass(frozen=True, slots=True)
class _ReviewerChoice:
    """Which model reviews, and what may stand in when it cannot run."""

    selected: str
    #: A reviewer was named (pinned or configured), so its outage is the person's to fix.
    named: bool
    pinned: bool
    fallback_enabled: bool
    #: The requester's own model; set only for an application review with fallback on.
    fallback_model: str | None


def _choose_reviewer(
    expected_catalog_id: str | None, requester_thread_id: str | None,
) -> _ReviewerChoice:
    """Pick the reviewer model: pinned, else configured, else the requester's."""
    from infra.database import get_db
    from infra.settings import (
        get_host_access_reviewer_fallback,
        get_host_access_reviewer_model,
    )

    db = get_db()
    try:
        fallback_enabled = get_host_access_reviewer_fallback(db=db)
        configured = get_host_access_reviewer_model(db=db)
        requester = _requester_model(db, requester_thread_id)
    finally:
        db.close()
    if expected_catalog_id is not None:
        return _ReviewerChoice(expected_catalog_id, True, True, fallback_enabled, None)
    if configured is not None:
        return _ReviewerChoice(
            configured, True, False, fallback_enabled,
            requester if fallback_enabled and requester != configured else None,
        )
    if requester is None:
        raise HostAccessSupervisorError(
            "host-access reviewer has no model: none is configured "
            "and the requesting thread has no selected model"
        )
    return _ReviewerChoice(requester, False, False, fallback_enabled, None)


async def _resolve_runnable(selected: str, requester_thread_id: str | None):
    """Resolve one exact reviewer model under the requester's locality policy, or raise why it cannot run now."""

    def resolve():
        """Resolve the exact target, then the account its review runs on if it may run."""
        from infra.database import get_db
        from llm.resolution import resolve_thread_target

        db = get_db()
        try:
            target = resolve_thread_target(
                selected, thread_id=requester_thread_id, db=db,
            )
            # A target policy already refused needs no account: resolving one
            # would either waste the read or raise the account's own cause and
            # bury the reason the person's setting gave.
            if target.available:
                target = replace(
                    target,
                    account_id=_requester_reviewer_account(db, target, requester_thread_id),
                )
        finally:
            db.close()
        if not target.available:
            raise ReviewerUnavailableError(
                "host-access reviewer model is unavailable",
                outage=ReviewerOutage(
                    _model_name(target),
                    target.availability_reason or "model_unavailable",
                ),
            )
        return target

    target = await run_in_db(resolve)
    if target.harness == "native":
        from llm.resolution import ModelUnavailableError, validate_native_target

        try:
            await validate_native_target(target)
        except ModelUnavailableError as exc:
            raise ReviewerUnavailableError(
                str(exc), outage=ReviewerOutage(_model_name(target), exc.reason),
            ) from exc
    return target


@dataclass(frozen=True, slots=True)
class FallbackReviewerTarget(ExecutionTarget):
    """The requester's own model, reviewing because the chosen reviewer could not run."""

    reviewer_fallback: bool = True


def _refusal(
    exc: ReviewerUnavailableError,
    choice: _ReviewerChoice,
    fallback: ReviewerOutage | None = None,
) -> ReviewerUnavailableError:
    """The outage, carrying whether fallback applies and what it hit."""
    return ReviewerUnavailableError(
        str(exc),
        outage=exc.outage,
        pinned=choice.pinned,
        fallback_enabled=choice.fallback_enabled,
        fallback=fallback,
    )


def _report_unavailable(exc: ReviewerUnavailableError, choice: _ReviewerChoice, thread_id: str | None) -> None:
    """Record that the chosen reviewer could not run, so outages and fallback use are measurable."""
    emit_product_event(
        "host_access_review",
        "reviewer_unavailable",
        thread_id=thread_id,
        properties={
            "cause": exc.outage.reason,
            "fallback_enabled": choice.fallback_enabled,
            "pinned": choice.pinned,
            "named": choice.named,
        },
    )


async def resolve_reviewer_target(
    expected_catalog_id: str | None = None,
    *,
    requester_thread_id: str | None = None,
):
    """Resolve the reviewer: pinned, else configured, else the requester's model.

    A configured reviewer that cannot run is replaced by the requester's own
    model only when the fallback setting is on, and only for an application
    review. A pinned (per-action) review keeps its exact binding and never
    falls back. Only "cannot run" falls back: a reviewer that ran and failed
    is never resolved here. A model the requester's project or chat setting
    has ruled out counts as one that cannot run, for the fallback too.
    """
    choice = await run_in_db(
        lambda: _choose_reviewer(expected_catalog_id, requester_thread_id)
    )
    try:
        return await _resolve_runnable(choice.selected, requester_thread_id)
    except ReviewerUnavailableError as exc:
        _report_unavailable(exc, choice, requester_thread_id)
        if not choice.named:
            # No reviewer was chosen: this is the requester's own model failing,
            # which Settings cannot fix, so it stays an ordinary review failure.
            raise HostAccessSupervisorError(str(exc)) from exc
        if choice.fallback_model is None:
            raise _refusal(exc, choice) from exc
        try:
            target = await _resolve_runnable(choice.fallback_model, requester_thread_id)
        except ReviewerUnavailableError as second:
            raise _refusal(exc, choice, second.outage) from second
    return FallbackReviewerTarget(
        **{field.name: getattr(target, field.name) for field in fields(ExecutionTarget)}
    )


async def resolve_bound_reviewer_target(
    expected_catalog_id: str | None,
    binding: ReviewerBinding,
    requester_thread_id: str | None,
    *,
    resolver=resolve_reviewer_target,
):
    """Re-resolve a reviewer and require its approved identity binding."""
    target = await resolver(
        expected_catalog_id,
        requester_thread_id=requester_thread_id,
    )
    require_reviewer_binding(target, binding)
    return target
