"""Release and wake mechanics for a user ask after its lifecycle ends."""
from __future__ import annotations

_PROVENANCE = "user.ask_cleared"


def release(
    db, artifact_id: str, why: str, *, wake_verb: str, retired: bool = False,
    ask_release=None,
) -> tuple[int, set[str]]:
    """Clear an ask's holds, reconcile plans, and report each ask-release executor."""
    from plans import admission_projection, holds, plan_wake, user_ask
    from plans.plan_events import append_owner_event
    from plans.user_steering import CHANGE_KIND
    from prompts import plan_wakes as wakes

    rows = db.execute(
        user_ask.open_asks_sql(
            "h.id AS hold_id, n.id AS node_id, n.plan_id, n.kind, n.title,"
            " n.revision, n.executor_thread_id, n.owner_thread_id,"
            " p.owner_kind, p.owner_id,"
            " EXISTS (SELECT 1 FROM plan_node_holds other"
            " WHERE other.node_id=n.id AND other.id<>h.id"
            " AND other.cleared_at IS NULL) AS other_holds,"
            " i.title AS ask",
            joins="JOIN artifact_identities i ON i.id = l.artifact_id",
        ) + " ORDER BY h.created_at, h.id",
        (artifact_id,),
    ).fetchall()
    detail = dict(ask_release.detail) if ask_release is not None else {}
    detail.update(user_ask.ask_endings.detail(why=why))
    causes = [
        holds.clear_write(
            row["node_id"], row["hold_id"], expected_kind="blocked",
            actor_id=None, provenance=_PROVENANCE, db=db,
            detail=detail or None,
        )
        for row in rows
    ]
    executors = {row["hold_id"]: admission_projection.executor_of(row, row) for row in rows}
    handoffs = _reconcile_for_askers(db, rows, executors)
    released_executors: set[str] = set()
    for row, (cause_id, cause_at) in zip(rows, causes):
        summary = _release_summary(row, wake_verb, retired=retired, ask_release=ask_release)
        executor = executors[row["hold_id"]]
        wake_id = None if executor is None else plan_wake.append(
            db,
            recipient=executor,
            kind=plan_wake.ASK_RELEASED,
            subject_id=str(row["node_id"]),
            revision=int(row["revision"]),
            about=str(row["hold_id"]),
            plan_id=str(row["plan_id"]),
            content=plan_wake.with_next(
                db, str(row["plan_id"]), executor,
                wakes.WAKE_ASK_RELEASED.format(summary=summary),
                **handoffs.pop((str(row["plan_id"]), executor), {}),
            ),
            open_node_id=str(row["node_id"]),
        )
        if wake_id is not None:
            released_executors.add(executor)
        append_owner_event(
            db,
            plan_id=str(row["plan_id"]),
            event_id=cause_id,
            actor_id=None,
            event_type=CHANGE_KIND,
            summary=summary,
            details={
                "node_id": str(row["node_id"]),
                "action": "ask_cleared",
                "ask_wake_id": wake_id,
            },
            created_at=cause_at,
        )
    if rows:
        from plans import notifications

        notifications.report_touched({str(row["plan_id"]) for row in rows}, db=db)
    return len(rows), released_executors


def _reconcile_for_askers(db, rows, executors: dict) -> dict[tuple[str, str], dict]:
    """Reconcile each plan an answer released, and return the askers' handoff facts.

    The ask wake is the asker's result of this write, so reconcile is told not
    to wake the asker again (``causing_executor``), and runs before the wake so
    its next block is the state the asker returns to. Standing in for the
    work-selected wake, the asker's first wake on each plan also carries what
    that wake would have: its facts, keyed by plan and asker (``selected_fact``).
    """
    from plans import admission, plan_wake

    handoffs: dict[tuple[str, str], dict] = {}
    for plan_id in dict.fromkeys(str(row["plan_id"]) for row in rows):
        causing = next(
            executors[row["hold_id"]] for row in rows if str(row["plan_id"]) == plan_id
        )
        reconciled = admission.reconcile_plan(
            plan_id, actor_id=None, provenance=_PROVENANCE, causing_executor=causing, db=db,
        )
        if causing is not None:
            handoffs[(plan_id, causing)] = plan_wake.selected_fact(db, reconciled, causing)
    return handoffs


def _release_summary(row, wake_verb: str, *, retired: bool, ask_release) -> str:
    """Return the one-line answer the wake and its receipt carry."""
    from prompts import plan_wakes as wakes

    detail = ask_release.detail if ask_release is not None else {}
    return wakes.ask_release_summary(
        ask=str(row["ask"] or "your ask"), done=wake_verb, retired=retired,
        kind=str(row["kind"]), title=str(row["title"]), node_id=str(row["node_id"]),
        verdict=detail.get("verdict"), note=detail.get("note"),
        other_holds=bool(row["other_holds"]),
    )
