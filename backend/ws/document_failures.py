"""Transport observations from canonical failure owners, without clearing their facts."""
import asyncio


async def failure_state(entry, *, reconcile: bool = False) -> dict:
    """Matched open alone requests explicit owner absence reconciliation."""
    records, threshold = await asyncio.to_thread(entry.live.store.failure_snapshot)
    blocking, clears = [], []
    for budget in (entry.publisher.budget, entry.lane.retry.budget):
        failure = records.get(budget.kind)
        if failure is not None and failure.attempts >= budget.limit:
            blocking.append({"kind": budget.kind, "message": failure.message,
                             "failure_generation": failure.generation})
        elif failure is None and reconcile:
            clears.append({"kind": budget.kind, "clears_through": threshold})
    if entry.policy is not None:
        owner = entry.policy.owner
        failure, revision = owner.setup_state(entry.live.path)
        if failure is not None:
            blocking.append({"kind": "setup_failed", "message": failure.reason,
                             "setup_incarnation": owner.setup_incarnation,
                             "setup_revision": failure.revision})
        elif reconcile:
            clears.append({"kind": "setup_failed", "setup_incarnation": owner.setup_incarnation,
                           "clears_through": revision})
    return {"blocking": blocking, **({"failure_clears": clears} if reconcile else {})}
