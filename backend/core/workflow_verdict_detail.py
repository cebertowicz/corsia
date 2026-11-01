"""Aggregates for the nightly verdict job's one log line: what the batch was made of and how it ended.

There is no supported route to read the live database, so the job's log line is how its owner reads the outcome
on real data. Everything here is a count, a size or a reason word computed inside the job over the open batch or the
open trials' window: no lesson, workflow, task, node, thread or person name or id leaves this module. Reads only.

Temporary: these aggregates answer the checks owed before verdict writes are enabled. Delete this module, the
fields it feeds and ``TrialRead`` before ``VERDICT_WRITES_ENABLED`` flips, so none of it sits on the write path.
"""
from __future__ import annotations

import json
import sqlite3
from collections import Counter
from typing import Any, Iterable, Mapping, Sequence

from core.workflow_verdict_evaluator import STORED
from core.workflow_verdict_read import json_ids
from core.workflow_verdict_types import Evaluation, split_subject
from core.workflow_verdicts import DEFAULT_MINIMUM

HALVES_CAP = 50
_KEYS = "keys AS (SELECT json_extract(value,'$[0]') AS lesson, json_extract(value,'$[1]') AS sha FROM json_each(:keys))"
# Every receipt row of the open trials, with its task's send-back count and the task it revises (a send-back's
# revision is a follow-up of the sent-back task and draws its own receipt).
_RECEIPTS_SQL = f"""
    WITH {_KEYS}, r AS (
      SELECT g.lesson_id AS lesson, g.lesson_behavioral_sha256 AS sha, g.node_id, g.variant,
        COALESCE(n.sent_back,0) AS sent_back, n.follow_up_of AS source
      FROM keys k JOIN guidance_loads g ON g.lesson_id=k.lesson AND g.lesson_behavioral_sha256=k.sha
        AND g.node_id IS NOT NULL
      LEFT JOIN plan_nodes n ON n.id=g.node_id),
    rev AS (
      SELECT r.*, (SELECT o.variant FROM r o WHERE o.lesson=r.lesson AND o.sha=r.sha AND o.node_id=r.source
                   LIMIT 1) AS source_variant,
        EXISTS(SELECT 1 FROM r o WHERE o.lesson=r.lesson AND o.sha=r.sha AND o.node_id=r.source) AS source_drew
      FROM r WHERE r.source IS NOT NULL
        AND EXISTS(SELECT 1 FROM plan_nodes s WHERE s.id=r.source AND s.sent_back>0))
    SELECT (SELECT COUNT(*) FROM r) AS receipt_rows,
      (SELECT COUNT(*) FROM r WHERE sent_back>0) AS on_sent_back_nodes,
      (SELECT COUNT(*) FROM (SELECT 1 FROM r GROUP BY lesson, sha, node_id HAVING COUNT(*)>1)) AS nodes_with_many,
      (SELECT COUNT(*) FROM rev) AS revision_receipts,
      (SELECT COUNT(*) FROM rev WHERE source_drew) AS revision_after_source_receipt,
      (SELECT COUNT(*) FROM rev WHERE source_drew AND source_variant IS NOT variant) AS revision_in_other_arm
"""
# A settled workflow run is its root task; a send-back's revision continues that run and is not a root of its own.
_RUNS_SQL = """
    SELECT COUNT(*) AS settled, COALESCE(SUM(r.sent_back>0),0) AS root_sent_back,
      COALESCE(SUM(EXISTS(SELECT 1 FROM plan_nodes v WHERE v.follow_up_of=r.id AND v.workflow_ref=r.workflow_ref)),0)
        AS continued_by_revision
    FROM plan_nodes r WHERE r.id IN (SELECT value FROM json_each(:ids))
"""
_DELEGATED_SQL = f"""
    WITH {_KEYS}, d AS (
      SELECT n.workflow_step IS NOT NULL AS step,
        (SELECT group_concat(DISTINCT COALESCE(g.variant,'none')) FROM guidance_loads g
         JOIN keys k ON g.lesson_id=k.lesson AND g.lesson_behavioral_sha256=k.sha WHERE g.node_id=n.id) AS variants
      FROM plan_nodes n WHERE n.spawned_thread_id IS NOT NULL AND n.created_at>=:since)
    SELECT step, variants, COUNT(*) AS nodes FROM d GROUP BY step, variants
"""


def kind_of(evaluation: Evaluation) -> str:
    """workflow or lesson, from the subject key."""
    return split_subject(evaluation.subject)[0]


def by_kind(open_counts: Mapping[str, int], evaluations: Sequence[Evaluation]) -> dict[str, dict[str, Any]]:
    """Open, evaluated and the verdict words, for workflows and lessons separately."""
    return {kind: {"open": open_counts.get(kind, 0),
                   "evaluated": sum(kind_of(item) == kind for item in evaluations),
                   "by_word": dict(Counter(item.verdict.word for item in evaluations if kind_of(item) == kind))}
            for kind in ("workflow", "lesson")}


def reached_detail(evaluations: Sequence[Evaluation]) -> list[dict[str, Any]]:
    """Each reached verdict: kind, word, reason, look and the sides' run counts its deciding measures used.

    The counts are the largest on each side among the deciding (not shown) measures, after the look's cut.
    """
    found = []
    for item in evaluations:
        verdict = item.verdict
        if verdict.word not in STORED:
            continue
        used = [call for call in verdict.calls if call.role != "shown" and (call.n_new or call.n_base)]
        found.append({"kind": kind_of(item), "word": verdict.word, "reason": verdict.reason, "look": verdict.look,
                      "new": max((call.n_new for call in used), default=None),
                      "base": max((call.n_base for call in used), default=None)})
    return found


def lesson_halves(sizes: Iterable[tuple[int, int]]) -> dict[str, Any]:
    """Each open lesson trial's (with tasks, without tasks) as the verdict counts them, largest first."""
    pairs = sorted(sizes, reverse=True)
    return {"lesson_halves": [list(pair) for pair in pairs[:HALVES_CAP]],
            "lesson_both_20": sum(min(pair) >= DEFAULT_MINIMUM for pair in pairs)}


def sent_back(db: sqlite3.Connection, keys: Sequence[tuple[str, str]], run_ids: Iterable[str]) -> dict[str, Any]:
    """Send-backs in the open trials' receipt rows and in the workflow batch's settled runs.

    A lesson trial counts a task sent back when its own row says so; the revision a send-back creates is a new task
    with its own receipt and its own draw. A workflow run is its root task, which the send-back closes.
    """
    arms = dict(db.execute(_RECEIPTS_SQL, {"keys": _keys(keys)}).fetchone()) if keys else {}
    runs = dict(db.execute(_RUNS_SQL, {"ids": json_ids(run_ids)}).fetchone())
    return {"lesson_arms": arms, "workflow_runs": runs}


def delegated_receipts(db: sqlite3.Connection, keys: Sequence[tuple[str, str]], since: str | None) -> dict[str, Any]:
    """Tasks handed to a delegated worker since the earliest open trial's first receipt, by whether they drew one.

    Nearest stored proxy for the worker-read drop: a delegated task with no receipt for any open trial. A task is
    counted once per arm it drew in. ``step`` is a task that is a workflow step, the only kind the worker-read
    delivery covers; ``other`` is every other delegated task.
    """
    out: dict[str, Any] = {name: {"with_receipt": 0, "without_receipt": 0, "by_variant": {}} for name in ("step", "other")}
    if not keys or since is None:
        return out
    for row in db.execute(_DELEGATED_SQL, {"keys": _keys(keys), "since": since}).fetchall():
        bucket = out["step" if row["step"] else "other"]
        if row["variants"] is None:
            bucket["without_receipt"] += row["nodes"]
            continue
        bucket["with_receipt"] += row["nodes"]
        for variant in str(row["variants"]).split(","):
            bucket["by_variant"][variant] = bucket["by_variant"].get(variant, 0) + row["nodes"]
    return out


def _keys(keys: Sequence[tuple[str, str]]) -> str:
    return json.dumps([list(key) for key in keys])
