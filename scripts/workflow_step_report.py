#!/usr/bin/env python3
"""Read-only grouped per-workflow-step report for a CORSIA SQLite database."""

from __future__ import annotations

import argparse
import json
import sqlite3
import sys
from collections import defaultdict
from pathlib import Path
from statistics import median

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "backend"))

GAP = "required-step definitions not persisted; missing required steps not computed"
ACCEPTED_STATES = {"accepted", "accepted_with_followup"}


def connect(path: str) -> sqlite3.Connection:
    """Open an existing database without ever creating or mutating it."""
    resolved = Path(path).expanduser().resolve()
    if not resolved.is_file():
        raise ValueError(f"--db must name an existing SQLite file: {resolved}")
    try:
        db = sqlite3.connect(f"file:{resolved}?mode=ro", uri=True)
        db.row_factory = sqlite3.Row
        db.execute("SELECT 1")
        return db
    except sqlite3.Error as exc:
        raise ValueError(f"cannot read SQLite database {resolved}: {exc}") from exc


def _nodes(db: sqlite3.Connection) -> list[dict]:
    """Fetch the complete graph before selecting workflow-tagged root chains."""
    return [dict(row) for row in db.execute(
        """SELECT id,plan_id,parent_id,follow_up_of,commitment_id,kind,state,description,
                  workflow_ref,workflow_version,workflow_step,difficulty,created_at
           FROM plan_nodes"""
    )]


def _attribution(db: sqlite3.Connection, ids: list[str]) -> dict[str, list[dict]]:
    if not ids:
        return {}
    marks = ",".join("?" for _ in ids)
    rows = db.execute(
        f"""SELECT plan_node_id,COALESCE(model,'unknown') AS model,
                   COALESCE(plan_usage_role,'unknown') AS role,
                   SUM(total_tokens) AS tokens,
                   SUM(total_tokens IS NULL) AS unknown_tokens
            FROM llm_usage WHERE plan_node_id IN ({marks})
            GROUP BY plan_node_id,model,role""", ids,
    ).fetchall()
    result: dict[str, list[dict]] = defaultdict(list)
    for row in rows:
        result[str(row["plan_node_id"])].append(dict(row))
    return result


def _corrections(db: sqlite3.Connection, ids: list[str]) -> dict[str, int]:
    if not ids:
        return {}
    marks = ",".join("?" for _ in ids)
    rows = db.execute(
        f"""SELECT json_extract(payload_json,'$.source_node_id') AS source,COUNT(*) AS count
            FROM plan_node_events WHERE event='correction_created'
              AND json_extract(payload_json,'$.source_node_id') IN ({marks}) GROUP BY source""", ids,
    ).fetchall()
    return {str(row["source"]): int(row["count"]) for row in rows}


def _revision_children(db: sqlite3.Connection) -> dict[str, list[str]]:
    """Return every further round of a step: rejection revisions and follow-ups that count as the step."""
    rows = db.execute(
        """SELECT n.id AS child_id,n.follow_up_of AS parent_id
           FROM plan_nodes n JOIN plan_nodes s ON s.id=n.follow_up_of
           LEFT JOIN plan_node_events e
             ON e.node_id=n.id AND e.event='revision_created' AND json_valid(e.payload_json)
             AND json_extract(e.payload_json,'$.source_node_id')=n.follow_up_of
           WHERE e.id IS NOT NULL OR (n.workflow_ref=s.workflow_ref AND n.workflow_step=s.workflow_step)"""
    ).fetchall()
    children: dict[str, list[str]] = defaultdict(list)
    for row in rows:
        children[str(row["parent_id"])].append(str(row["child_id"]))
    return children


def _chain(root: dict, children: dict[str, list[str]], by_id: dict[str, dict]) -> list[dict]:
    """Walk a verified rejection-revision chain in outcome order."""
    result: list[dict] = []

    def visit(node: dict) -> None:
        result.append(node)
        for child_id in sorted(children.get(node["id"], ()), key=lambda item: (by_id[item]["created_at"], item)):
            visit(by_id[child_id])

    visit(root)
    return result


def _attempt_counts(db: sqlite3.Connection, ids: list[str]) -> dict[str, int]:
    if not ids:
        return {}
    marks = ",".join("?" for _ in ids)
    rows = db.execute(
        f"SELECT node_id,COUNT(*) AS count FROM plan_node_attempts WHERE node_id IN ({marks}) GROUP BY node_id", ids,
    ).fetchall()
    return {str(row["node_id"]): int(row["count"]) for row in rows}


def _chain_attempts(counts: dict[str, int], ids: list[str], final_state: str) -> dict:
    """Count attempts on precisely the nodes that make up one step revision chain."""
    attempts = sum(counts.get(node_id, 0) for node_id in ids)
    return {"total": attempts,
            "first_accepted": len(ids) == 1 and attempts == 1 and final_state in ACCEPTED_STATES}


def _sum_amounts(amounts: list[dict]) -> dict:
    """Keep known totals and unknown-row counts separate; unknown is never zero."""
    known = [amount["known"] for amount in amounts if amount["known"] is not None]
    return {"known": sum(known) if known else None,
            "unknown_rows": sum(int(amount.get("unknown_rows", 0)) for amount in amounts)}


def _run_id(node: dict, all_nodes: dict[str, dict]) -> str:
    current = node
    while current["parent_id"] and current["parent_id"] in all_nodes:
        current = all_nodes[current["parent_id"]]
    return current["id"]


def _spread(values: list[int | float], *, run_count: int) -> dict:
    """Return counts only until this step has six distinct workflow runs."""
    result = {"n": len(values)}
    if run_count >= 6 and values:
        result.update({"median": median(values), "range": [min(values), max(values)]})
    else:
        result["counts_only"] = True
    return result


def _group_metrics(nodes: list[dict]) -> dict:
    """Summarize canonical node totals exactly once for a workflow-step group."""
    run_count = len({node["run_id"] for node in nodes})
    known_costs = [node["cost"]["known"] for node in nodes if node["cost"]["known"] is not None]
    known_tokens = [node["tokens"]["known"] for node in nodes if node["tokens"]["known"] is not None]
    seconds = [node["seconds"]["seconds"] for node in nodes]
    attempts = [node["attempts"]["total"] for node in nodes]
    return {
        "task_count": len(nodes), "cost": _spread(known_costs, run_count=run_count), "cost_known_total": sum(known_costs), "cost_unknown_rows": sum(node["cost"]["unknown_rows"] for node in nodes),
        "tokens": _spread(known_tokens, run_count=run_count), "tokens_known_total": sum(known_tokens), "tokens_unknown_rows": sum(node["tokens"]["unknown_rows"] for node in nodes),
        "time_seconds": _spread(seconds, run_count=run_count), "time_seconds_total": sum(seconds), "attempts": _spread(attempts, run_count=run_count), "attempts_total": sum(attempts),
        "first_attempt_accepted": sum(node["first_attempt_accepted"] for node in nodes),
        "corrections": sum(node["corrections"] for node in nodes),
        "revisions": sum(node["revisions"] for node in nodes),
        "rework_tokens": _spread([node["rework_tokens"]["known"] for node in nodes if node["rework_tokens"]["known"] is not None], run_count=run_count),
        "rework_tokens_known_total": sum(node["rework_tokens"]["known"] or 0 for node in nodes),
        "rework_tokens_unknown_rows": sum(node["rework_tokens"]["unknown_rows"] for node in nodes),
        "failed_or_closed_without_completion": sum(node["failed_or_closed_without_completion"] for node in nodes),
        "attribution_mismatch_nodes": [node["task_id"] for node in nodes if node["attribution_mismatch"]],
    }


def _model_metrics(nodes: list[dict]) -> list[dict]:
    """Group own-only usage by model without repeating rolled totals."""
    grouped: dict[str, list[dict]] = defaultdict(list)
    for node in nodes:
        for row in node["attribution"]:
            grouped[row["model"]].append(row)
    result = []
    for model, rows in sorted(grouped.items()):
        tokens = sum(row["tokens"] or 0 for row in rows)
        correction_tokens = sum((row["tokens"] or 0) for row in rows if row["role"] == "correction")
        result.append({"model": model, "scope": "own-only", "tokens_known": tokens,
                       "tokens_unknown_rows": sum(row["unknown_tokens"] for row in rows),
                       "correction_token_share": correction_tokens / tokens if tokens else None,
                       "roles": {role: sum((row["tokens"] or 0) for row in rows if row["role"] == role)
                                 for role in sorted({row["role"] for row in rows})}})
    return result


def _runs(task_nodes: list[dict], all_nodes: dict[str, dict], siblings: dict) -> list[dict]:
    grouped: dict[str, list[dict]] = defaultdict(list)
    for node in task_nodes:
        grouped[_run_id(node, all_nodes)].append(node)
    result = []
    for run_id, nodes in grouped.items():
        phase = all_nodes.get(run_id, {})
        skips = [line.removeprefix("skipped: ").strip() for line in str(phase.get("description") or "").splitlines()
                 if line.strip().startswith("skipped: ")]
        key = (nodes[0]["workflow_ref"], nodes[0]["workflow_version"])
        present = {node["workflow_step"] for node in nodes}
        result.append({"run_id": run_id, "workflow_ref": key[0], "workflow_version": key[1], "skipped": skips,
                       "added_steps": sorted(step for step in present if step.startswith("+")),
                       "absent_vs_sibling_runs": sorted(siblings[key] - present - {skip.split(",", 1)[0] for skip in skips})})
    return result


def report(db: sqlite3.Connection, workflow: str | None = None) -> dict:
    """Return one canonical record per workflow step revision chain."""
    from plans import node_cost

    required = {"plan_nodes", "llm_usage", "plan_node_events", "plan_node_attempts", "plans"}
    present = {row[0] for row in db.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    missing = sorted(required - present)
    if missing:
        raise ValueError("not a CORSIA database; missing tables: " + ", ".join(missing))
    all_nodes = _nodes(db)
    by_id = {node["id"]: node for node in all_nodes}
    task_nodes = [node for node in all_nodes if node["kind"] == "task"]
    children = _revision_children(db)
    revision_ids = {child_id for ids in children.values() for child_id in ids}
    roots = [node for node in task_nodes if node["id"] not in revision_ids and node["workflow_ref"] is not None
             and (workflow is None or node["workflow_ref"] == workflow)]
    costs = node_cost.project_many({node["plan_id"] for node in task_nodes}, db=db)
    usage = _attribution(db, [node["id"] for node in task_nodes])
    corrections = _corrections(db, [node["id"] for node in task_nodes])
    attempt_counts = _attempt_counts(db, [node["id"] for node in task_nodes])
    records = []
    siblings: dict[tuple[str, str | None], set[str]] = defaultdict(set)
    for root in roots:
        chain = _chain(root, children, by_id)
        chain_ids = [node["id"] for node in chain]
        own_totals = [costs[node["plan_id"]][node["id"]]["own"] for node in chain]
        rolled_totals = [costs[node["plan_id"]][node["id"]]["rolled"] for node in chain]
        rows = [row for node_id in chain_ids for row in usage.get(node_id, [])]
        own_tokens = _sum_amounts([total["tokens"] for total in own_totals])
        # Tokens alone reconcile: the cost is Measurement's API-equivalent price, not a ledger sum.
        mismatch = (sum(row["tokens"] or 0 for row in rows) != (own_tokens["known"] or 0)
                    or sum(row["unknown_tokens"] for row in rows) != own_tokens["unknown_rows"])
        final = chain[-1]
        # An agent may close a follow-up it no longer needs; the step it followed stays accepted.
        accepted = any(node["state"] in ACCEPTED_STATES for node in chain)
        attempt = _chain_attempts(attempt_counts, chain_ids, final["state"])
        records.append({"task_id": root["id"], "chain_node_ids": chain_ids, "run_id": _run_id(root, by_id),
                        "workflow_ref": root["workflow_ref"], "workflow_version": root["workflow_version"],
                        "step": root["workflow_step"], "difficulty": root["difficulty"],
                        "tokens": _sum_amounts([total["tokens"] for total in rolled_totals]),
                        "cost": _sum_amounts([total["model_cost_usd"] for total in rolled_totals]),
                        "seconds": {"seconds": sum(total["execution"]["seconds"] for total in rolled_totals)},
                        "attempts": attempt, "first_attempt_accepted": attempt["first_accepted"],
                        "corrections": sum(corrections.get(node_id, 0) for node_id in chain_ids),
                        "revisions": len(chain) - 1,
                        "rework_tokens": _sum_amounts([costs[node["plan_id"]][node["id"]]["rolled"]["tokens"] for node in chain[1:]]),
                        "failed_or_closed_without_completion": final["state"] == "failed" or (final["state"] == "closed" and not accepted),
                        "attribution": rows, "attribution_mismatch": mismatch})
        siblings[root["workflow_ref"], root["workflow_version"]].add(root["workflow_step"])
    groups: dict[tuple, list[dict]] = defaultdict(list)
    for record in records:
        groups[(record["workflow_ref"], record["workflow_version"], record["step"], record["difficulty"])].append(record)
    result_groups = []
    for key, members in sorted(groups.items(), key=lambda item: _nullable_key(item[0])):
        result_groups.append({"workflow_ref": key[0], "workflow_version": key[1], "step": key[2], "difficulty": key[3],
                              "run_count": len({node["run_id"] for node in members}), "metrics": _group_metrics(members),
                              "models": _model_metrics(members), "node_totals_scope": "rolled, once per step revision chain (not repeated per model)"})
    runs = _runs(roots, by_id, siblings)
    empty_reason = "no workflow-tagged tasks in this CORSIA database" if not records else None
    return {"header": {"required_step_gap": GAP, "attribution": "own-only model/role token split reconciled to own token rollup across each revision chain; mismatches are visible",
                        "cost_basis": "API-equivalent price at list rates, step totals only",
                        "conclusion_rule": "fewer than 6 runs: counts only, no conclusions", "units": {"cost": "USD", "time_seconds": "seconds", "tokens": "tokens"},
                        "matched_task_count": len(records), "empty_reason": empty_reason}, "groups": result_groups, "runs": runs}


def _nullable_key(values: tuple) -> tuple:
    """Order a key whose parts may be null (unrecorded versions, older rows): null first."""
    return tuple((value is not None, value or "") for value in values)


def _compare(result: dict, workflow: str) -> dict:
    commits = sorted({group["workflow_version"] for group in result["groups"]}, key=lambda value: _nullable_key((value,)))
    if len(commits) != 2:
        raise ValueError(f"--compare {workflow} requires exactly two workflow_version values; found {len(commits)}")
    grouped: dict[tuple, dict] = defaultdict(dict)
    for row in result["groups"]:
        # JSON renders a null key as "null"; name it so here, where mixed keys would not sort.
        grouped[row["step"], row["difficulty"]][row["workflow_version"] or "null"] = row
    return {"workflow_ref": workflow, "versions": commits,
            "steps": [{"step": key[0], "difficulty": key[1], "by_version": versions}
                      for key, versions in sorted(grouped.items(), key=lambda item: _nullable_key(item[0]))]}


def _text(result: dict) -> str:
    lines = [f"workflow step report — {GAP}", result["header"]["conclusion_rule"]]
    for group in result["groups"]:
        metrics = group["metrics"]
        lines.append(f"{group['workflow_ref']} {group['workflow_version']} {group['step']} {group['difficulty']}: "
                     f"runs={group['run_count']} tasks={metrics['task_count']} cost={metrics['cost']} tokens={metrics['tokens']} "
                     f"time={metrics['time_seconds']} attempts={metrics['attempts']} first_accept={metrics['first_attempt_accepted']} "
                     f"corrections={metrics['corrections']} revisions={metrics['revisions']} rework_tokens={metrics['rework_tokens']} "
                     f"failed_or_closed={metrics['failed_or_closed_without_completion']}")
        for model in group["models"]:
            lines.append(f"  {model['model']} own-only: tokens={model['tokens_known']} "
                         f"correction_share={model['correction_token_share']}")
    for run in result["runs"]:
        lines.append(f"run {run['run_id']}: skipped={run['skipped']} added={run['added_steps']} absent_vs_sibling={run['absent_vs_sibling_runs']}")
    if result.get("compare"):
        comparison = result["compare"]
        lines.append(f"compare {comparison['workflow_ref']}: versions={comparison['versions']}")
        lines.extend(f"  {row['step']} {row['difficulty']}: {sorted(row['by_version'])}" for row in comparison["steps"])
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Read-only grouped workflow-step report")
    parser.add_argument("--db", required=True, help="path to a SQLite database; no database is read implicitly")
    parser.add_argument("--json", action="store_true", help="emit JSON")
    parser.add_argument("--compare", metavar="WORKFLOW", help="compare exactly two versions of one workflow")
    args = parser.parse_args(argv)
    try:
        with connect(args.db) as db:
            result = report(db, args.compare)
            if args.compare:
                result["compare"] = _compare(result, args.compare)
    except ValueError as exc:
        parser.error(str(exc))
    print(json.dumps(result, indent=2, sort_keys=True, default=str) if args.json else _text(result))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
