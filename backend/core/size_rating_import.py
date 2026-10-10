"""Validate and import size-rating facts inside the caller's transaction.

Every row is checked against the current canonical outcome before anything is
written: the node must be an accepted outcome in the explicit scope, and its plan,
revision, latest evidence event and source snapshot hash must still be the ones
the rating was made from. One bad row refuses the whole input, so a failure never
leaves a partial import. Nothing here commits; the caller owns the transaction and
commits only on an explicit apply.
"""

from __future__ import annotations

import hashlib
import json
import re
from collections import Counter
from typing import Any

from api.measurement_queries import outcome_ctes
from core.measurement_scope import query_params, scope_cte, validate_scope
from core.size_rating import (
    BASES, FACT_KEYS, SHA256_KEYS, SIZE_CLASSES, SIZE_EVENT, current_snapshots, subject_key,
)
from infra.outcome_event_attributes import filter_attributes
from infra.outcome_events import record_outcome_event

_HEX64 = re.compile(r"[0-9a-f]{64}")
# Every accepted week: the import admits a node wherever it settled, and the
# served series decides which weeks a request shows.
_ALL_WEEKS = {"from": "0001-01-01", "to": "9999-12-24"}


class SizeImportRefusal(ValueError):
    """The input was refused as a whole; nothing was written."""

    def __init__(self, problems: list[str]):
        super().__init__("; ".join(problems[:20]) + (f" (+{len(problems) - 20} more)" if len(problems) > 20 else ""))
        self.problems = problems


def _eligible(db, user_id: str, project: str, agent: str) -> dict[str, tuple[str, str, str, str | None]]:
    """Map each accepted outcome in scope to (plan, settled at, owner thread, project)."""
    scope = validate_scope(db, user_id, project, agent)
    params = query_params({**scope.params(user_id), **_ALL_WEEKS})
    rows = db.execute(scope_cte() + outcome_ctes() + """
        SELECT o.id,n.plan_id,s.settled_at,o.root_id,t.project_id FROM outcome_rows o
        JOIN settle s ON s.node_id=o.id JOIN plan_nodes n ON n.id=o.id
        LEFT JOIN threads t ON t.id=o.root_id
    """, params).fetchall()
    return {row[0]: (row[1], row[2], row[3], row[4]) for row in rows}


def _row_problems(row: Any) -> list[str]:
    if not isinstance(row, dict):
        return ["is not an object"]
    problems = []
    keys = set(row)
    if keys != set(FACT_KEYS):
        missing, extra = sorted(set(FACT_KEYS) - keys), sorted(keys - set(FACT_KEYS))
        problems.append(f"fields differ (missing {missing}, unexpected {extra})")
        return problems
    if row["size_class"] not in SIZE_CLASSES:
        problems.append("size_class is not a known class")
    if row["basis"] not in BASES:
        problems.append("basis is not a known basis")
    for key in SHA256_KEYS:
        if not isinstance(row[key], str) or not _HEX64.fullmatch(row[key]):
            problems.append(f"{key} is not a lowercase SHA-256")
    kept, dropped = filter_attributes(SIZE_EVENT, row)
    if dropped or kept != row:
        problems.append(f"attributes would be dropped or rewritten: {sorted(dropped) or 'sanitized'}")
    return problems


def _identity_problems(row: dict[str, Any], eligible, snapshot) -> list[str]:
    if row["node_id"] not in eligible or snapshot is None:
        return ["node is not an accepted outcome in this scope"]
    problems = []
    if row["plan_id"] != snapshot.plan_id:
        problems.append("plan differs from the node's plan")
    if row["node_revision"] != snapshot.node_revision:
        problems.append("node revision is stale")
    if row["evidence_event_id"] != snapshot.evidence_event_id:
        problems.append("latest evidence event differs")
    if row["basis"] != snapshot.basis:
        problems.append("basis differs from the node's current evidence")
    if row["source_snapshot_sha256"] != snapshot.sha256():
        problems.append("source snapshot hash differs")
    return problems


def dedupe_key(row: dict[str, Any], supersedes: tuple[str, ...] = ()) -> str:
    """One key per rating payload and the newest observations it supersedes.

    A node's first rating keys on its payload alone. A later rating also names the
    event ids it replaces, so re-importing an earlier or retracted rating is a new
    observation rather than a collision with the old, immutable one.
    """
    payload = json.dumps(row, sort_keys=True, separators=(",", ":"))
    if supersedes:
        payload += "|" + ",".join(supersedes)
    return "size_rating:" + hashlib.sha256(payload.encode("utf-8")).hexdigest()


def _newest(db, node_id: str) -> list[tuple[str, str | None, str | None]]:
    """Every fact tied at the node's newest observation time, as the served series reads them."""
    return db.execute(
        "SELECT id,attributes,retracted_at FROM outcome_events WHERE subject_key=? AND event_name=? "
        "AND observed_ts=(SELECT MAX(observed_ts) FROM outcome_events WHERE subject_key=? AND event_name=?) "
        "ORDER BY id", (subject_key(node_id), SIZE_EVENT, subject_key(node_id), SIZE_EVENT),
    ).fetchall()


def _serves(newest: list[tuple[str, str | None, str | None]], row: dict[str, Any]) -> bool:
    """Whether the node's newest facts already serve exactly this rating, live and unconflicted.

    Distinctness is read on the raw (attributes text, retracted) pair, as the served
    series reads it: equal values stored in a different key order are still a conflict.
    """
    distinct = {(attributes, retracted is not None) for _, attributes, retracted in newest}
    if len(distinct) != 1:
        return False
    attributes, retracted = next(iter(distinct))
    try:
        return not retracted and json.loads(attributes or "") == row
    except ValueError:
        return False


def import_size_ratings(db, rows: list[Any], *, user_id: str, project: str, agent: str,
                        apply: bool) -> dict[str, Any]:
    """Validate every row, then write the new facts when ``apply`` is true.

    Raises ``SizeImportRefusal`` before any write when one row fails. Returns
    aggregate counts only: no task text, ids or ratings leave this function.
    """
    eligible = _eligible(db, user_id, project, agent)
    node_ids = [row["node_id"] for row in rows if isinstance(row, dict) and isinstance(row.get("node_id"), str)]
    snapshots = current_snapshots(db, sorted(set(node_ids) & set(eligible)))
    problems: list[str] = []
    seen: Counter[str] = Counter(node_ids)
    for number, row in enumerate(rows, 1):
        found = _row_problems(row)
        if not found:
            found = _identity_problems(row, eligible, snapshots.get(row["node_id"]))
            if seen[row["node_id"]] > 1:
                found.append("node appears more than once in the input")
        problems.extend(f"row {number}: {problem}" for problem in found)
    if not rows:
        problems.append("input has no rows")
    if problems:
        raise SizeImportRefusal(problems)

    # Unchanged means the node's newest facts already serve this rating. A match
    # anywhere in its history is not enough: a newer rating, a retraction or an
    # equal-time conflict still governs, so the import appends a new observation.
    pending = []
    for row in rows:
        newest = _newest(db, row["node_id"])
        if not _serves(newest, row):
            pending.append((row, dedupe_key(row, tuple(event_id for event_id, _, _ in newest))))
    written = 0
    if apply:
        for row, key in pending:
            plan_id, settled_at, owner, project_id = eligible[row["node_id"]]
            result = record_outcome_event(
                db, dedupe_key=key, event_name=SIZE_EVENT, source="corsia", provenance="imported",
                ts=settled_at, subject_key=subject_key(row["node_id"]), project_id=project_id,
                agent_thread_id=owner, attributes=row,
            )
            if not result.inserted or result.dropped_keys:
                raise RuntimeError("size rating write did not land intact; roll the transaction back")
            written += 1
    return {
        "rows": len(rows), "would_write": len(pending), "written": written,
        "unchanged": len(rows) - len(pending),
        "classes": {name: sum(row["size_class"] == name for row in rows) for name in SIZE_CLASSES},
        "basis": {name: sum(row["basis"] == name for row in rows) for name in BASES},
    }
