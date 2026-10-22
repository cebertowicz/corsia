"""Why a selected plan node is not moving: a provider reset, or a stalled executor.

A selected node reads running, but its executor may be unable to run: a
provider-reset liveness row waits on the reset, or the executor is stalled
(``wait_causes.stalled_sql``: its head is held for its model, or its last Turn
failed with nothing queued, #3317). Either way the row reads waiting and names
why, never Working.
"""

from __future__ import annotations

from datetime import datetime, timezone

from automation import provider_reset_liveness
from infra.packed_rows import fetch_all

_STALLED = {
    "model": "Stalled: its model is unavailable",
    "account": "Stalled: its input is held for its account",
    "error": "Stalled: its last Turn failed",
    "locality": "Stalled: local models only; its model can't run",
}


def executor_facts(executor_ids: set[str], *, db) -> dict[str, dict]:
    """Read reset and stall facts for executors in bounded SQLite batches."""
    from connections import read_account_readiness
    from plans.wait_causes import account_read_snapshot, stalled_sql

    facts: dict = provider_reset_liveness.nodes(executor_ids, db=db)
    ordered = sorted(executor_ids)
    for start in range(0, len(ordered), 900):
        batch = ordered[start:start + 900]
        marks = ",".join("?" for _ in batch)
        with account_read_snapshot(db):
            rows = fetch_all(
                db,
                f"SELECT t.id, {stalled_sql('t.id')} AS why FROM threads AS t"
                f" WHERE t.id IN ({marks})", batch)
            readiness = read_account_readiness([str(r['id']) for r in rows if r['why'] == 'account'], db=db)
        for row in rows:
            if row["why"] is not None:
                thread_id = str(row["id"])
                facts[thread_id] = {**facts.get(thread_id, {}), "stalled": row["why"]}
                if row["why"] == "account":
                    facts[thread_id]["account_readiness"] = readiness.get(thread_id)
    return facts


def nodes(node_plans, executor_by_node: dict, executions: dict, *, db) -> dict:
    """Adapt full projections to shared selected-executor facts."""
    executor_ids = {
        executor_by_node[node["id"]]
        for node, _plan in node_plans
        if executor_by_node[node["id"]] is not None
        and executions[node["id"]]["selection"] is not None
    }
    return executor_facts(executor_ids, db=db)


def reason_from_facts(
    fact: dict | None,
    *,
    plan_id: str,
    node_id: str,
    attempt_id: str | None,
    condition: str,
) -> str | None:
    """Name why one still-selected executor is not moving from canonical facts."""
    if fact is None or attempt_id is None or condition != "running":
        return None
    if fact.get("stalled") == "account":
        from prompts.plan_wakes import account_readiness_detail

        return _STALLED["account"] + ' ' + account_readiness_detail(fact.get('account_readiness'))
    if "provider_reset_at" in fact:
        if (
            str(fact["plan_id"]) != plan_id
            or str(fact["plan_node_id"]) != node_id
            or str(fact["plan_attempt_id"]) != attempt_id
        ):
            return None
        return _reason(str(fact["provider_reset_at"]))
    if "stalled" in fact:
        return _STALLED[fact["stalled"]]
    return None


def at(fact: dict | None, node, plan, execution: dict) -> str | None:
    """Adapt full execution envelopes to the shared fact matcher."""
    selection = execution["selection"]
    return reason_from_facts(
        fact,
        plan_id=str(plan["id"]),
        node_id=str(node["id"]),
        attempt_id=None if selection is None else str(selection["attempt_id"]),
        condition=str(execution["condition"]),
    )


def _reason(reset_at: str) -> str:
    """Name a stored reset deadline as an exact UTC instant.

    Plan text carries every moment in ISO 8601 so a reader can render it: an
    agent reads it as is, and the pane shows it in the viewer's own time.
    """
    moment = datetime.fromisoformat(reset_at).astimezone(timezone.utc)
    return f"Provider resets at {moment.isoformat(timespec='seconds')}"
