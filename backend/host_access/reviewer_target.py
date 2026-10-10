"""Which model reviews a host-access lease.

A pinned reviewer (the model that reviewed the application) wins; otherwise
the configured reviewer; otherwise the requesting thread's own model. The
requester's model supplies only the model identity: every review still runs as
one fresh isolated call over the application payload, never a fork of the
requester's transcript.
"""

from __future__ import annotations

from dataclasses import dataclass, replace

from host_access.errors import HostAccessSupervisorError
from infra.db_async import run_in_db


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
        raise HostAccessSupervisorError(
            f"host-access reviewer account unavailable: {reason}"
        ) from exc


async def resolve_reviewer_target(
    expected_catalog_id: str | None = None,
    *,
    requester_thread_id: str | None = None,
):
    """Resolve the reviewer: pinned, else configured, else the requester's model."""

    def resolve():
        """Pick the reviewer model and resolve its exact available target."""
        from infra.database import get_db
        from infra.settings import get_host_access_reviewer_model
        from llm.resolution import resolve_execution_target

        db = get_db()
        try:
            selected = expected_catalog_id
            if selected is None:
                selected = get_host_access_reviewer_model(
                    db=db
                ) or _requester_model(db, requester_thread_id)
                if selected is None:
                    raise HostAccessSupervisorError(
                        "host-access reviewer has no model: none is configured "
                        "and the requesting thread has no selected model"
                    )
            target = resolve_execution_target(selected, db=db)
            target = replace(
                target,
                account_id=_requester_reviewer_account(db, target, requester_thread_id),
            )
        finally:
            db.close()
        if not target.available:
            raise HostAccessSupervisorError(
                "host-access reviewer model is unavailable"
            )
        return target

    target = await run_in_db(resolve)
    if target.harness == "native":
        from llm.resolution import validate_native_target

        await validate_native_target(target)
    return target


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
