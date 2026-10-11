"""Compose selected-work wakes through the canonical plan wake writer."""

from __future__ import annotations


def notify(
    db, reconciled, *, plan_id: str, causing_executor: str | None,
    selection_wake_summaries: dict[str, str] | None = None, from_worker: bool = False,
) -> None:
    """Tell executors whose slots reconciliation newly filled about their work."""
    from plans import delegated_selection, handoff_brief, plan_wake
    from prompts import plan_wakes as wakes

    for node_id, executor in reconciled.selected:
        if not executor or executor == causing_executor:
            continue
        node = plan_wake._node(db, node_id)
        if delegated_selection.told_elsewhere(db, executor, node, from_worker=from_worker):
            continue
        summary = (selection_wake_summaries or {}).get(node_id)
        line = wakes.WAKE_WORK_SELECTED.format(title=str(node["title"]))
        if summary is not None:
            line = f"{line}\n{wakes.WAKE_ASK_RELEASED.format(summary=summary)}"
        plan_wake.append(
            db,
            recipient=executor,
            kind=plan_wake.WORK_SELECTED,
            subject_id=node_id,
            revision=int(node["revision"]),
            plan_id=plan_id,
            content=plan_wake.with_next(
                db, plan_id, executor, line,
                **handoff_brief.fact(db, node_id, executor),
            ),
        )
