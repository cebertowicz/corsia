"""Cumulative cost of one original structured-plan commitment."""

from __future__ import annotations

from collections import defaultdict
from datetime import datetime, timezone
from typing import Any

from infra.packed_rows import fetch_all
from infra.turn_usage_remainder import pending_measurement_counts
from plans import service, usage_pricing
from plans.node_cost import settle_known, sum_known

#: The release reasons that mean a try ended in failure. The one definition of
#: friction: the commitment's failed attempts and a plan row's retry count.
FAILED_RELEASES = frozenset({"failed", "review_rejected"})
#: Approved 2026-09-16: attempt 2 already reads as friction; waking on the
#: third leaves one normal retry before interrupting the parent
#: (``plans.delegation_notice``, matrix rows C1-C3).
FRICTION_WAKE_ATTEMPTS = 3
_SETTLED_TURNS = frozenset({"completed", "error"})


def _parse(value: str) -> datetime:
    parsed = datetime.fromisoformat(value)
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)


def _elapsed(nodes, now: datetime) -> dict[str, Any]:
    root = next((row for row in nodes if row["id"] == row["commitment_id"]), None)
    if root is None:
        raise service.PlanError("original plan commitment is unavailable")
    open_nodes = [
        row
        for row in nodes
        if row["archived_at"] is None
        and row["state"] not in service._TERMINAL_STATES
    ]
    end = now if open_nodes else max(_parse(row["updated_at"]) for row in nodes)
    seconds = max(0, int((end - _parse(root["created_at"])).total_seconds()))
    return {"known": seconds, "status": "in_progress" if open_nodes else "complete"}


def _rows_by(rows, field: str) -> dict[str, list]:
    indexed: dict[str, list] = defaultdict(list)
    for row in rows:
        indexed[str(row[field])].append(row)
    return dict(indexed)


def _attempt_summary(rows) -> dict[str, int]:
    terminal_failures = {
        max(
            (row for row in rows if row["node_id"] == node_id),
            key=lambda row: row["attempt_ordinal"],
        )["id"]
        for node_id in {
            row["node_id"] for row in rows if row["node_state"] == "failed"
        }
    }
    return {
        "total": len(rows),
        "failed": sum(
            row["release_reason"] in FAILED_RELEASES
            or (
                row["id"] in terminal_failures
                and row["release_reason"] not in FAILED_RELEASES
            )
            for row in rows
        ),
        "correction": sum(bool(row["is_correction"]) for row in rows),
        "delegated": sum(
            not row["is_correction"]
            and row["parent_plan_id"] is not None
            and row["commitment_id"] != row["node_id"]
            for row in rows
        ),
    }


def _unmeasured_rows(turns, usage, pending: dict[str, int]) -> int:
    """Rows no total can include, counted per row rather than per Turn.

    A Turn is many provider calls, each its own ledger row. Asking whether a
    Turn has *any* row would call a Turn measured while one of its calls is
    still a pending receipt, and present a total that is complete-looking and
    short; so each measurement the ledger owes this commitment counts as one
    unknown, whichever Turn holds it. A settled Turn bound here holding
    neither a row nor a receipt for it has no measurement at all, and counts
    as one.
    """
    measured = {row["turn_id"] for row in usage if row["turn_id"] is not None}
    unknown = sum(pending.values())
    for row in turns:
        if (
            str(row["id"]) not in pending
            and row["id"] not in measured
            and row["status"] in _SETTLED_TURNS
        ):
            unknown += 1
    return unknown


def _load(commitment_ids: list[str], *, db) -> dict[str, Any]:
    marks = ",".join("?" for _ in commitment_ids)
    params = tuple(commitment_ids)
    roots = db.execute(
        f"""SELECT id,commitment_id FROM plan_nodes
            WHERE id IN ({marks})""",
        params,
    ).fetchall()
    valid = {
        str(row["id"])
        for row in roots
        if row["id"] == row["commitment_id"]
    }
    missing = set(commitment_ids) - valid
    if missing:
        raise service.PlanError("commitment id must name the original obligation")

    nodes = fetch_all(
        db, f"""SELECT * FROM plan_nodes
            WHERE commitment_id IN ({marks}) ORDER BY commitment_id,created_at,id""",
        params,
    )
    turns = fetch_all(
        db, f"""SELECT id,status,plan_commitment_id FROM active_turns
            WHERE plan_commitment_id IN ({marks})""",
        params,
    )
    attempts = fetch_all(
        db, f"""SELECT a.*,n.state AS node_state,n.commitment_id,p.parent_plan_id,
                   EXISTS (
                     SELECT 1 FROM plan_node_events e
                     WHERE e.node_id=a.node_id AND e.event='correction_created'
                   ) AS is_correction
            FROM plan_node_attempts a
            JOIN plan_nodes n ON n.id=a.node_id
            JOIN plans p ON p.id=n.plan_id
            WHERE n.commitment_id IN ({marks})
            ORDER BY n.commitment_id,a.selected_at,a.id""",
        params,
    )
    # What the ledger still owes is read before what it holds, and that order
    # is the guarantee: the projection writing a call's row deletes its receipt
    # in the same transaction, so one committing between these two reads is
    # briefly counted unknown by a row that covers it -- never missed by both
    # and reported short. A receipt is owed by its own call's binding, which
    # names an attempt, so a commitment never selected is owed none and a plan
    # of idle work costs no read.
    pending = pending_measurement_counts(db, commitment_ids) if attempts else {}
    # One row per commitment and Turn, not per call; a plan write reads no cost (#2489).
    usage = usage_pricing.priced_usage(
        db, ("plan_commitment_id", "turn_id"), commitment_ids,
        lambda sql, ids: fetch_all(
            db, sql.format(marks=",".join("?" for _ in ids)), tuple(ids),
        ),
    )
    effort = {
        str(row["commitment_id"]): int(row["count"])
        for row in db.execute(
            f"""SELECT COALESCE(
                       json_extract(result_json,'$.node.commitment_id'),
                       json_extract(result_json,'$.commitment_id'),
                       json_extract(result_json,'$.correction.commitment_id')
                     ) AS commitment_id,
                     COUNT(*) AS count
                FROM plan_mutation_receipts
                WHERE actor_id LIKE 'user:%'
                  AND commitment_id IN ({marks})
                GROUP BY commitment_id""",
            params,
        ).fetchall()
    }
    return {
        "nodes": _rows_by(nodes, "commitment_id"),
        "usage": _rows_by(usage, "plan_commitment_id"),
        "turns": _rows_by(turns, "plan_commitment_id"),
        "attempts": _rows_by(attempts, "commitment_id"),
        "effort": effort,
        "recording_failures": _recording_failures(db, marks, params),
        "pending": pending,
    }


def _recording_failures(db, marks: str, params: tuple) -> dict[str, int]:
    """Calls that began but could not be recorded, per commitment."""
    return {
        str(row["commitment_id"]): int(row["count"])
        for row in db.execute(
            f"""SELECT n.commitment_id, COUNT(*) AS count
                FROM plan_node_events e JOIN plan_nodes n ON n.id=e.node_id
                WHERE e.event='usage_recording_failed'
                  AND n.commitment_id IN ({marks})
                GROUP BY n.commitment_id""",
            params,
        ).fetchall()
    }


def project_many(
    commitment_ids,
    *,
    db=None,
    now: datetime | None = None,
) -> dict[str, dict[str, Any]]:
    """Aggregate many commitments with a bounded query count."""

    ids = sorted({str(value) for value in commitment_ids})
    if not ids:
        return {}
    clock = now or datetime.now(timezone.utc)
    with service._connection(db) as connection:
        loaded = _load(ids, db=connection)
        result = {}
        for commitment_id in ids:
            nodes = loaded["nodes"].get(commitment_id, [])
            usage = loaded["usage"].get(commitment_id, [])
            turns = loaded["turns"].get(commitment_id, [])
            attempts = loaded["attempts"].get(commitment_id, [])
            unmeasured = _unmeasured_rows(
                turns, usage, loaded["pending"].get(commitment_id, {}),
            )
            result[commitment_id] = settle_known({
                "commitment_id": commitment_id,
                "attempts": _attempt_summary(attempts),
                "tokens": {
                    "known": sum_known(row["tokens"] for row in usage),
                    "unknown_rows": (
                        sum(row["unknown_tokens"] for row in usage)
                        + unmeasured
                    ),
                },
                "model_cost_usd": {
                    "known": sum_known(row["cost"] for row in usage),
                    "unknown_rows": (
                        sum(row["unknown_cost"] for row in usage)
                        + unmeasured
                    ),
                },
                # Calls that began but could not be recorded: a defect, so no
                # total presented here is silently short.
                "usage_recording_failures": loaded["recording_failures"].get(
                    commitment_id, 0,
                ),
                "elapsed_seconds": _elapsed(nodes, clock),
                "user_effort": {
                    "known_plan_actions": loaded["effort"].get(commitment_id, 0),
                    "status": "partial",
                },
            }, turns=len(turns))
        return result


def project(
    commitment_id: str,
    *,
    db=None,
    now: datetime | None = None,
) -> dict[str, Any]:
    """Aggregate each durable usage/attempt row exactly once by commitment id."""

    return project_many([commitment_id], db=db, now=now)[commitment_id]
