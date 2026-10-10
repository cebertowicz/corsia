"""Transactional actor-bound plan watch operations."""

from __future__ import annotations

from dataclasses import asdict

from plans import admission, boundary_fields as fields, service


def _validated_condition(condition):
    """Classify a watch condition against the one defer-condition contract.

    ``automation.watches`` reports a malformed condition and a watch already
    bound elsewhere as the same ``WatchError``, and that class is outside the
    plan taxonomy entirely — so both used to escape this boundary uncaught and
    reach the caller as an unhandled 500. Shape is decided here, before
    dispatch, using `registry.attention`'s validator rather than a second copy
    of the rules, so a caller-fixable condition is a caller-fixable refusal.
    """
    from registry.attention import AttentionError, validate_defer_condition

    try:
        return validate_defer_condition(condition)
    except AttentionError as exc:
        raise service.PlanInputError(str(exc)) from exc


def _binding(params, actor, db) -> str | None:
    """Return the artifact this wait is about, refusing one the actor cannot see.

    An artifact is identified by its integration, type and external id together,
    so a wait may name only an artifact that already exists: a binding to an
    unknown string records none of that identity, and two integrations can use
    the same external id. It must also be an artifact this actor owns, because
    the owner is the one thread the router delivers the artifact's events to,
    and the wait ends only when its event reaches the thread that armed it. A
    subscriber sees those events as digest facts, never as a delivery, so a
    wait it armed could end only at its deadline.

    A binding also has to say what ends the wait. The event is what resolves the
    watch when it arrives; without it the wait names something to watch and
    nothing that could stop it.
    """
    artifact_id = params.get("artifact_id")
    if artifact_id is None:
        return None
    from plans import artifact_links

    # ``artifact_id`` is the artifact's own id, as it is everywhere else in
    # plans, and the shared lookup resolves nothing else.
    known, owner_thread = artifact_links.identity_owner(artifact_id, db=db)
    if not known:
        raise service.PlanInputError(
            "artifact_id must be the id of an artifact CORSIA already knows"
        )
    if not actor.thread_id or owner_thread != actor.thread_id:
        raise service.PlanInputError(
            "artifact_id must name an artifact this thread owns"
        )
    event = params.get("expected_event")
    if not isinstance(event, str) or not event.strip():
        raise service.PlanInputError(
            "a watch on an artifact needs the expected_event that ends it"
        )
    return artifact_id.strip()


def record_resolved(
    db, node_id: str, *, watch_id: str, expected_event, actor_id,
    reason: str | None = None, evidence: str | None = None,
):
    """Record on the node's own history that the wait it was holding has ended.

    The one writer for that fact, called by each plans-side path that actually
    ends a wait: the plan tool below, and the delivered event that the watch
    was armed for (``plans.watch_resolution``). Automation owns the watch
    lifecycle and holds no reference to plans, so the plans domain records
    what a watch meant for its own node rather than being told.

    Teardown is deliberately not a caller: archiving a node or retiring a
    worker releases a watch, but the wait did not end -- the work went away.
    """
    service._event(
        db, node_id, "watch_resolved", actor_id=actor_id,
        provenance="plan_watch_resolved",
        payload={
            "watch_id": watch_id, "expected_event": expected_event,
            **({"reason": reason} if reason else {}),
            **({"evidence": evidence} if evidence else {}),
        },
    )


def arm_write(plan_id: str, params, actor, db):
    """Arm a plan-bound watch and release a selection it made invalid.

    The caller reconciles (``finish_arm``). Returns the armed watch.
    """

    from automation import watches
    from plans import event_wait

    if event_wait.is_event_wait(params):
        # Every event wait ends: its deadline is set here, where every plan
        # wait is armed (W4).
        params = event_wait.with_deadline(params, db=db, owner=actor.thread_id)
    condition = _validated_condition(params.get("condition"))
    artifact_id = _binding(params, actor, db)
    superseded = _resolve_past_deadline_watches(db, params["node_id"])
    try:
        watch = watches.arm_watch(
            artifact_id if artifact_id is not None else params["node_id"],
            condition,
            owner_thread=actor.thread_id,
            expected_event=params.get("expected_event"),
            plan_id=plan_id,
            plan_node_id=params["node_id"],
            armed_by_actor_id=actor.audit_id,
            db=db,
        )
    except watches.UnboundWatchError as exc:
        # A wait on no artifact that no deadline ends: the caller can fix it.
        raise service.PlanInputError(watches.UNBOUND_REFUSED) from exc
    except watches.WatchError as exc:
        # The shape was accepted above, so what is left is a live watch already
        # bound to another plan actor: the state of the target moved, and
        # re-reading is the remedy.
        raise service.PlanError(str(exc)) from exc
    # The armed watch is a waiting cause. Release only a selection it actually
    # made invalid (re-arming an already-resolved watch keeps a valid one), so
    # attempt history names the watch.
    from plans import admission_attempts, selection_validity

    attempt = admission_attempts.open_attempt(db, params["node_id"])
    if attempt is not None and not selection_validity.selection_valid(db, attempt):
        admission.release(params["node_id"], "watch_armed", db=db)
    record_armed(
        db, params["node_id"], watch, blocked=params.get("blocked"),
        scheduled=params.get("scheduled"), superseded_watch_ids=superseded,
        actor_id=actor.audit_id, provenance="plan_watch_armed",
    )
    return watch


def _resolve_past_deadline_watches(db, node_id: str) -> list[str]:
    """Resolve superseded armed deadlines before recording the replacement wait."""
    from datetime import datetime, timezone

    from automation import watches
    from registry import attention

    rows = db.execute(
        """SELECT id, condition_value FROM expected_event_watches
           WHERE plan_node_id=? AND status='armed' AND condition_type='deadline'
           ORDER BY id""",
        (node_id,),
    ).fetchall()
    # Parsed, never compared as strings: the canonical defer contract
    # (watches.due_watches) decides what "passed" means.
    now = datetime.now(timezone.utc)
    ids = []
    for row in rows:
        try:
            deadline = attention.validate_defer_condition(
                {"type": "deadline", "value": row["condition_value"]},
            )["value"]
        except (attention.AttentionError, ValueError, TypeError):
            continue
        if datetime.fromisoformat(deadline) <= now:
            ids.append(str(row["id"]))
    for watch_id in ids:
        watches.resolve_watch(watch_id, db=db)
    return ids


def _optional_note(params, field: str) -> str | None:
    """Store supplied nonblank free text within the existing feedback budget."""
    value = params.get(field)
    if value is None or (isinstance(value, str) and not value.strip()):
        return None
    from plans.close_next_bounds import FEEDBACK_MAX_BYTES, bounded

    return bounded(fields.text(params, field), FEEDBACK_MAX_BYTES)


def record_armed(
    db, node_id: str, watch, *, blocked: dict | None, actor_id: str | None,
    provenance: str, scheduled: dict | None = None,
    superseded_watch_ids: list[str] | None = None,
) -> None:
    """Record on the node's own history that a wait began: the one writer.

    A blocker armed as a wait carries who clears it and why
    (``plans.blocked_wait``), and a scheduled task why it was set aside
    (``plans.scheduled_wait``); the watch row has no place for either.
    """
    service._event(
        db, node_id, "watch_armed", actor_id=actor_id, provenance=provenance,
        payload={
            "watch_id": watch.id, "expected_event": watch.expected_event,
            "condition": watch.condition,
            **({"blocked": blocked} if blocked else {}),
            **({"scheduled": scheduled} if scheduled else {}),
            **({"superseded_watch_ids": superseded_watch_ids}
               if superseded_watch_ids else {}),
        },
    )


def finish_arm(plan_id: str, params, actor, watch, db) -> dict:
    """Reconcile after arming a watch, refresh liveness, and return the projection."""

    from automation import plan_liveness

    admission.reconcile_plan(
        plan_id, actor_id=actor.audit_id, provenance="plan_watch_armed",
        causing_executor=actor.thread_id if actor.kind == "thread" else None,
        db=db,
    )
    plan_liveness.on_turn_complete(
        actor.thread_id,
        db=db,
    )
    result = admission.result(params["node_id"], db=db)
    result["watch"] = asdict(watch)
    return result


def arm(plan_id: str, params, actor, db) -> dict:
    """Arm a plan-bound watch inside the outer mutation transaction."""

    watch = arm_write(plan_id, params, actor, db)
    return finish_arm(plan_id, params, actor, watch, db)


def resolve(plan_id: str, params, actor, db) -> dict:
    """Resolve only a watch bound to this plan, node, and owner thread."""

    from automation import plan_liveness, watches

    watch_id = params.get("watch_id")
    if not isinstance(watch_id, str) or not watch_id.strip():
        raise service.PlanInputError("watch_id is required")
    try:
        watches.resolve_watch(
            watch_id.strip(),
            plan_id=plan_id,
            plan_node_id=params["node_id"],
            owner_thread=actor.thread_id,
            db=db,
        )
        watch = watches.get_watch(watch_id.strip(), db=db)
    except watches.WatchError as exc:
        raise PermissionError(
            "watch is unavailable to the current plan actor"
        ) from exc
    record_resolved(
        db, params["node_id"], watch_id=watch.id,
        expected_event=watch.expected_event, actor_id=actor.audit_id,
        reason=_optional_note(params, "reason"),
        evidence=_optional_note(params, "evidence"),
    )
    admission.reconcile_plan(
        plan_id, actor_id=actor.audit_id, provenance="plan_watch_resolved",
        causing_executor=actor.thread_id if actor.kind == "thread" else None,
        db=db,
    )
    plan_liveness.on_turn_complete(
        actor.thread_id,
        db=db,
    )
    result = admission.result(params["node_id"], db=db)
    result["watch"] = asdict(watch)
    return result
