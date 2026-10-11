"""Compose selected-work wakes through the canonical plan wake writer."""

from __future__ import annotations


def notify(
    db, reconciled, *, plan_id: str, causing_executor: str | None,
    selection_wake_summaries: dict[str, str] | None = None, from_worker: bool = False,
) -> set[str]:
    """Tell executors whose slots reconciliation newly filled about their work.

    Return only node ids whose selected-work wake was actually accepted, so a
    caller with release-specific news can cover any deliberately skipped wake.
    """
    from plans import delegated_selection, handoff_brief, plan_wake
    from prompts import plan_wakes as wakes

    woken: set[str] = set()
    for node_id, executor in reconciled.selected:
        if not executor or executor == causing_executor:
            continue
        node = plan_wake.read_node(db, node_id)
        if delegated_selection.told_elsewhere(db, executor, node, from_worker=from_worker):
            continue
        summary = (selection_wake_summaries or {}).get(node_id)
        line = wakes.WAKE_WORK_SELECTED.format(title=str(node["title"]))
        if summary is not None:
            line = f"{line}\n{summary}"
        if plan_wake.append(
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
        ) is not None:
            woken.add(node_id)
    return woken
