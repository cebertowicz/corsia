"""Parent disposition API + enforcement (coordination MVP S5b).

The parent owns the final gate on whether a child/agent ask reaches the user. An
Artifact Review Profile (S5a) supplies findings/constraint DATA only; the parent
records one of seven durable outcomes here. A blocking profile failure prevents
``approve_to_user`` unless an explicit authorized override (authority + reason) is
recorded. Route-back to the child owner uses the Event Delivery model and is
never user-visible; only ``approve_to_user`` raises the artifact toward the user.

No artifact is archived and no subscription is mutated by a disposition (that is
Artifacts lifecycle / S6 closeout). ``reroute_owner`` uses the explicit audited
rehome path; ``defer_until`` records a concrete condition S7 can arm.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from events.disposition import delivery_disposition
from infra.database import write_transaction
from infra.product_events import emit_product_event
from registry import artifact_record as AR
from registry import models
from registry.ownership import rehome_artifact

APPROVE_TO_USER = "approve_to_user"
REJECT_WITH_REASON = "reject_with_reason"
ANSWER_INTERNALLY = "answer_internally"
REQUEST_REVISION = "request_revision"
REROUTE_OWNER = "reroute_owner"
DEFER_UNTIL = "defer_until"
SUPERSEDE = "supersede"

OUTCOMES = frozenset({
    APPROVE_TO_USER, REJECT_WITH_REASON, ANSWER_INTERNALLY, REQUEST_REVISION,
    REROUTE_OWNER, DEFER_UNTIL, SUPERSEDE,
})

#: Outcomes that route a result back to the child owner (never user-visible).
_ROUTE_BACK = frozenset({
    REJECT_WITH_REASON, ANSWER_INTERNALLY, REQUEST_REVISION, SUPERSEDE,
})


class DispositionError(ValueError):
    """An invalid disposition request (bad outcome / missing required input)."""


class BlockingProfileError(PermissionError):
    """approve_to_user attempted over a blocking profile failure without an
    explicit authorized override (PRG33)."""


@dataclass(frozen=True)
class DispositionResult:
    """The durable outcome of a parent disposition."""

    artifact_id: str
    outcome: str
    user_visible: bool                 # True only for approve_to_user
    attention_state: str               # raised | resolved | deferred | superseded | open
    routed_to: str | None = None       # child owner / new owner thread
    route_disposition: str | None = None   # Event-Delivery label for the route-back
    reason: str | None = None
    audit: dict[str, Any] = field(default_factory=dict)


def _resolve(artifact_id: str, db) -> dict:
    row = AR.identity_row_for(artifact_id, db=db)
    if row is None:
        raise DispositionError(f"artifact {artifact_id!r} is not registered")
    return {"id": row["id"], "integration": row["integration"],
            "type": row["type"], "external_id": row["external_id"]}


def _record_state(identity: dict, patch: dict, db) -> None:
    from registry import attention

    attention.record_attention(db, identity, patch)


def _clear_ask(identity: dict, child_owner: str | None, outcome: str,
               release, db) -> bool:
    """End the artifact's open ask; return whether that already woke the child owner."""
    from plans import public as plans_public

    ask_release_executors: set[str] = set()
    plans_public.ask_cleared(
        str(identity["id"]), outcome, release=release, db=db,
        released_executors=ask_release_executors,
    )
    return child_owner is not None and child_owner in ask_release_executors


def _route_back(identity: dict, child_owner: str | None, outcome: str,
                reason: str | None, owner_woken: bool, db) -> str | None:
    """Record an owner-visible (NOT user-visible) route-back to the child owner.

    A child owner the ask's release already woke with the outcome gets no
    second route-back.
    """
    if child_owner is None:
        return None
    label = None
    if not owner_woken:
        label = delivery_disposition("owner", "actionable")  # owner_wake
        models.record_artifact_change(
            artifact_id=identity["id"],
            internal_event="artifact.disposition", external_id=identity["external_id"],
            summary=f"parent disposition: {outcome}" + (f" — {reason}" if reason else ""),
            provenance={"outcome": outcome, "reason": reason,
                        "route_disposition": label, "user_visible": False}, db=db)
    emit_product_event("parent_disposition", outcome,
                       properties={"external_id": identity["external_id"],
                                   "child_owner": child_owner})
    return label


def _override_authorized(actor_thread: str | None, identity: dict, db) -> bool:
    """True only when ``actor_thread`` is a real conductor with authority here.

    Reuses the existing conductor-authority model (infra.conductor) — the same one
    registry ownership transfer uses — so override authority cannot be
    self-asserted by passing an arbitrary string. The CORSIA agent, or the
    project conductor of the artifact owner's scope, is authorized.
    """
    if not actor_thread:
        return False
    from infra import conductor, corsia_agent
    if corsia_agent.get_corsia_agent(db=db) == actor_thread:
        return True
    owner = AR.register_or_resolve(identity, db=db).owner_thread
    if owner is None:
        return False
    row = db.execute("SELECT project_id FROM threads WHERE id=?", (owner,)).fetchone()
    project_id = row["project_id"] if row else None
    return project_id is not None and conductor.get_project_conductor(project_id, db=db) == actor_thread


def _enforce_blocking(artifact_id: str, identity: dict, review_result,
                      override_blocking: bool, authority: str | None,
                      reason: str | None, actor_thread: str | None, db) -> dict:
    """approve_to_user gate: a blocking profile failure halts unless an AUTHORIZED
    override (concrete conductor actor + reason) is recorded."""
    from parent_review.profiles import run_review_profile
    result = review_result if review_result is not None else run_review_profile(
        artifact_id, db=db)
    if not result.blocking_failure:
        return {"blocking_failure": False}
    if not (override_blocking and reason and actor_thread):
        raise BlockingProfileError(
            f"artifact {artifact_id!r} failed a blocking review profile; "
            "approve_to_user requires an authorized override (override_blocking + "
            "reason + a concrete actor_thread)")
    if not _override_authorized(actor_thread, identity, db):
        raise BlockingProfileError(
            f"actor {actor_thread!r} is not an authorized conductor for this "
            "artifact; override authority cannot be self-asserted")
    return {"blocking_failure": True, "override_authority": authority,
            "override_reason": reason, "override_actor": actor_thread}


def dispose(
    artifact_id: str, outcome: str, *, reason: str | None = None,
    answer: str | None = None, authority: str | None = None,
    override_blocking: bool = False, new_owner: str | None = None,
    condition: dict | None = None, replacement_id: str | None = None,
    hold_ids: list[str] | None = None, require_hold_ids: bool = False, review_result=None,
    actor_thread: str | None = None, db=None,
    drives: list[str] | None = None,
) -> DispositionResult:
    """Record a parent disposition and route its outcome durably (never silent).

    The artifact's review profile constrains but does not decide; the parent's
    outcome here is authoritative. No outcome archives the artifact/subscription.
    """
    if outcome not in OUTCOMES:
        raise DispositionError(f"unknown disposition outcome {outcome!r}")
    if outcome == REROUTE_OWNER:
        return _do_reroute(
            artifact_id, new_owner, authority, reason, actor_thread, hold_ids,
            require_hold_ids, db,
        )
    from plans import public as plans_public

    # One write: the wakes collected are this disposition's alone, and a failed
    # ask release leaves no outcome recorded.
    with write_transaction(db, scope="parent_dispose") as _db, \
            plans_public.collect_wake_recipients(_db, drives):
        identity = _resolve(artifact_id, _db)
        record = AR.register_or_resolve(
            {"integration": identity["integration"], "type": identity["type"],
             "external_id": identity["external_id"]}, db=_db)
        child_owner = record.owner_thread
        from doceditor import review_bar

        review_bar.assert_current_ask(
            _db, str(identity["id"]), hold_ids, require_hold_ids=require_hold_ids,
        )
        if outcome == APPROVE_TO_USER:
            return _do_approve(identity, artifact_id, review_result,
                               override_blocking, authority, reason, actor_thread, _db)
        if outcome == DEFER_UNTIL:
            return _do_defer(identity, artifact_id, child_owner, condition, reason, _db)
        if outcome == SUPERSEDE:
            return _do_supersede(identity, artifact_id, child_owner,
                                 replacement_id, reason, _db)
        # reject_with_reason / answer_internally / request_revision
        return _do_route_back(identity, artifact_id, child_owner, outcome,
                              reason or answer, _db)


def _do_approve(identity, artifact_id, review_result, override_blocking,
                authority, reason, actor_thread, db) -> DispositionResult:
    audit = _enforce_blocking(artifact_id, identity, review_result,
                              override_blocking, authority, reason, actor_thread, db)
    patch = {"attention_state": "raised", "disposition_outcome": APPROVE_TO_USER,
             "disposition_audit": audit}
    _record_state(identity, patch, db)
    emit_product_event("parent_disposition", APPROVE_TO_USER,
                       properties={"external_id": identity["external_id"]})
    return DispositionResult(artifact_id, APPROVE_TO_USER, user_visible=True,
                             attention_state="raised", audit=audit, reason=reason)


def _do_route_back(identity, artifact_id, child_owner, outcome, reason,
                   db) -> DispositionResult:
    state = {REJECT_WITH_REASON: "resolved", ANSWER_INTERNALLY: "resolved",
             REQUEST_REVISION: "revision_requested"}[outcome]
    from plans import public as plans_public

    _record_state(identity, {"attention_state": state,
                             "disposition_outcome": outcome,
                             "disposition_reason": reason}, db)
    owner_woken = _clear_ask(
        identity, child_owner, outcome,
        plans_public.AskRelease({"note": reason}) if reason else None, db,
    )
    label = _route_back(identity, child_owner, outcome, reason, owner_woken, db)
    return DispositionResult(artifact_id, outcome, user_visible=False,
                             attention_state=state, routed_to=child_owner,
                             route_disposition=label, reason=reason)


def _do_reroute(artifact_id, new_owner, authority, reason,
                actor_thread, hold_ids, require_hold_ids, db) -> DispositionResult:
    with write_transaction(db, scope="parent_reroute_owner") as connection:
        identity = _resolve(artifact_id, connection)
        child_owner = AR.register_or_resolve(identity, db=connection).owner_thread
        from doceditor import review_bar

        review_bar.assert_current_ask(
            connection, str(identity["id"]), hold_ids, require_hold_ids=require_hold_ids,
        )
        if not new_owner:
            raise DispositionError("reroute_owner requires new_owner")
        if not (reason and actor_thread):
            raise DispositionError(
                "reroute_owner requires reason and a concrete actor_thread")
        # Ownership transfer needs VALIDATED authority — the same concrete-conductor
        # check as the approve override. A client-supplied authority string is not
        # enough; otherwise any /dispose caller could seize artifacts.
        if not _override_authorized(actor_thread, identity, connection):
            raise DispositionError(
                f"actor {actor_thread!r} is not an authorized conductor to reroute "
                "this artifact")
        rehome_artifact(
            integration=identity["integration"], type=identity["type"],
            external_id=identity["external_id"], new_owner_thread_id=new_owner,
            actor_thread_id=actor_thread, authority=authority, reason=reason,
            source_path="parent_review.disposition.reroute_owner", db=connection)
        models.update_metadata(
            identity["integration"], identity["type"], identity["external_id"],
            {"disposition_outcome": REROUTE_OWNER, "reroute_old_owner": child_owner,
             "reroute_new_owner": new_owner, "disposition_reason": reason},
            db=connection)
        return DispositionResult(artifact_id, REROUTE_OWNER, user_visible=False,
                                 attention_state="open", routed_to=new_owner,
                                 reason=reason,
                                 audit={"old_owner": child_owner, "new_owner": new_owner,
                                        "authority": authority})


def _do_defer(identity, artifact_id, child_owner, condition, reason,
              db) -> DispositionResult:
    # Reuse the single defer-condition contract (registry.attention); map its
    # AttentionError into this layer's DispositionError.
    from registry.attention import AttentionError, validate_defer_condition
    try:
        condition = validate_defer_condition(condition)  # normalized (canonical UTC)
    except AttentionError as exc:
        raise DispositionError(str(exc)) from exc
    patch = {"attention_state": "deferred",
             "disposition_outcome": DEFER_UNTIL,
             "defer_condition": condition,
             "disposition_reason": reason}
    _record_state(identity, patch, db)
    return DispositionResult(artifact_id, DEFER_UNTIL, user_visible=False,
                             attention_state="deferred", routed_to=child_owner,
                             reason=reason, audit={"condition": condition})


def _do_supersede(identity, artifact_id, child_owner, replacement_id, reason,
                  db) -> DispositionResult:
    if not replacement_id:
        raise DispositionError("supersede requires replacement_id")
    from plans import public as plans_public

    _record_state(identity, {"attention_state": "superseded",
                             "disposition_outcome": SUPERSEDE,
                             "superseded_by": replacement_id,
                             "disposition_reason": reason}, db)
    owner_woken = _clear_ask(
        identity, child_owner, SUPERSEDE,
        plans_public.AskRelease({"note": reason}) if reason else None, db,
    )
    label = _route_back(identity, child_owner, SUPERSEDE, reason, owner_woken, db)
    return DispositionResult(artifact_id, SUPERSEDE, user_visible=False,
                             attention_state="superseded", routed_to=child_owner,
                             route_disposition=label, reason=reason,
                             audit={"superseded_by": replacement_id})
