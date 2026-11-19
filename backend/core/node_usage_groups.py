"""Each plan node's ledger usage, grouped at write as Measurement classifies it, and kept exact.

``infra.node_usage_schema`` defines the table. This module owns everything that
depends on Measurement's classification SQL:

* the triggers on ``llm_usage`` that move a row's contribution in the same
  transaction as every insert, update of a column that keys or sums it, and
  delete, whichever code (or foreign-key action) writes the ledger;
* ``definition()``, a hash of that SQL, so a release that changes how rows are
  classified replaces the triggers and rebuilds the rows;
* ``ensure``, which does that before the server serves, from ``init_db``, in
  short transactions over ``plan_node_id`` ranges, recording how far it got;
* ``ready``, which tells a reader which nodes the current definition covers, so
  every other node is read from the ledger as before;
* ``recount``, the independent check: Measurement's own grouped read over the
  ledger, which the stored rows must equal.

Prices and model identity are never stored: readers classify the stored groups
with ``core.measurement_pricing`` exactly as they classify ledger groups.
"""

from __future__ import annotations

import hashlib
import logging
import time
from dataclasses import dataclass
from functools import cache
from typing import Any

from core.measurement_pricing import admission_sql, idle_hours_sql, input_tier_sql
from core.measurement_token_contract import TOKEN_BUCKETS
from infra.node_usage_schema import NODE_GROUPS_TABLE, NODE_USAGE_TABLES

log = logging.getLogger(__name__)

#: Ledger rows per rebuild transaction. A batch's write hold grows with the rows it
#: reads and groups (about 0.1-0.2 s at this size on a 900k-row ledger; see the PR evidence).
REBUILD_ROWS = 10_000

_KEY = ("plan_node_id", "plan_commitment_id", "turn_id", "model", "context_manifest_id")
_CLASSES = ("admission", "input_tier", "idle_hours")
_SUMS = (*TOKEN_BUCKETS, "total_tokens", "total_known", "total_odd")
#: ``UPDATE OF`` columns: every ``llm_usage`` column a group's key, class or sums read.
_WATCHED = (
    "id", "plan_node_id", "plan_commitment_id", "turn_id", "model", "context_manifest_id", "source",
    "created_at", *TOKEN_BUCKETS, "total_tokens",
)
TRIGGERS = ("llm_usage_node_groups_insert", "llm_usage_node_groups_update", "llm_usage_node_groups_delete")


def _classes(alias: str) -> tuple[str, str, str]:
    return admission_sql(alias), input_tier_sql(alias), idle_hours_sql(alias)


def _group_key(alias: str) -> str:
    """``group_key`` of the row named ``alias``: its nullable key columns, each ``quote()``d, comma-joined."""
    return "||','||".join(f"quote({alias}.{name})" for name in _KEY[1:])


def _row_sums(alias: str, admission: str | None = None) -> tuple[str, ...]:
    """What one ledger row adds to its group's sums, in ``_SUMS`` order.

    ``admission`` is an already computed admission to test instead of recomputing it.
    """
    admission = admission or admission_sql(alias)
    total = f"{alias}.total_tokens"
    return (
        *(f"(CASE WHEN ({admission})='ok' THEN {alias}.{bucket} ELSE 0 END)" for bucket in TOKEN_BUCKETS),
        f"(CASE WHEN typeof({total})='integer' THEN {total} ELSE 0 END)",
        f"(typeof({total})='integer')",
        f"({total} IS NOT NULL AND typeof({total})<>'integer')",
    )


#: Writes name ``llm_usage_node_groups`` (``NODE_GROUPS_TABLE``) literally: the plan-aggregate
#: writer gate (tests/test_plan_aggregate_input_writers.py) flags any write whose table is a placeholder.
_COLUMNS = ("plan_node_id", "group_key", *_CLASSES, *_KEY[1:], "records", *_SUMS, "last_at")


def _add(alias: str) -> str:
    """One upsert adding the row named ``alias`` to its group, creating the group when absent.

    The classes are computed once, in the inner select; the primary key finds the group.
    """
    values = ",".join((f"{alias}.plan_node_id", _group_key(alias), *(f"c.{name}" for name in _CLASSES),
                       *(f"{alias}.{name}" for name in _KEY[1:]), "1", *_row_sums(alias, "c.admission"),
                       f"{alias}.created_at"))
    classes = ",".join(f"{expression} AS {name}" for name, expression in zip(_CLASSES, _classes(alias)))
    sums = ",".join(f"{name}={name}+excluded.{name}" for name in _SUMS)
    return f"""
        INSERT INTO llm_usage_node_groups({','.join(_COLUMNS)}) SELECT {values} FROM (SELECT {classes}) c WHERE true
        ON CONFLICT(plan_node_id,group_key,{','.join(_CLASSES)}) DO UPDATE SET
          records=records+1,{sums},last_at=MAX(last_at,excluded.last_at);"""


def _remove(alias: str) -> str:
    """Statements taking the row named ``alias`` out of its group, dropping the group at zero rows.

    The matched group's own ``admission`` column is the row's admission, so it is not recomputed for the sums.
    """
    key = f"plan_node_id={alias}.plan_node_id AND group_key={_group_key(alias)}"
    classes = " AND ".join(f"{name}=({expression})" for name, expression in zip(_CLASSES, _classes(alias)))
    sums = ",".join(f"{name}={name}-{amount}" for name, amount in zip(_SUMS, _row_sums(alias, "admission")))
    return f"""
        UPDATE llm_usage_node_groups SET records=records-1,{sums} WHERE {key} AND {classes};
        DELETE FROM llm_usage_node_groups WHERE {key} AND records=0;"""


@cache
def trigger_sql() -> dict[str, str]:
    """The three triggers, by name."""
    insert, update, delete = TRIGGERS
    return {
        insert: f"CREATE TRIGGER {insert} AFTER INSERT ON llm_usage WHEN NEW.plan_node_id IS NOT NULL BEGIN"
                f"{_add('NEW')}\nEND",
        update: f"CREATE TRIGGER {update} AFTER UPDATE OF {','.join(_WATCHED)} ON llm_usage"
                " WHEN OLD.plan_node_id IS NOT NULL OR NEW.plan_node_id IS NOT NULL BEGIN"
                f"{_remove('OLD')}{_add('NEW')}\nEND",
        delete: f"CREATE TRIGGER {delete} AFTER DELETE ON llm_usage WHEN OLD.plan_node_id IS NOT NULL BEGIN"
                f"{_remove('OLD')}\nEND",
    }


def _grouped_select(where: str) -> str:
    """The stored rows recomputed from the ledger rows ``where`` picks (alias ``u``)."""
    sums = ",".join(f"SUM({amount}) AS {name}" for name, amount in zip(_SUMS, _row_sums("u")))
    keys = ",".join(f"u.{name} AS {name}" for name in _KEY[1:])
    classes = ",".join(f"{expression} AS {name}" for name, expression in zip(_CLASSES, _classes("u")))
    return f"""
        SELECT u.plan_node_id AS plan_node_id,{_group_key('u')} AS group_key,{classes},{keys},
          COUNT(*) AS records,{sums},MAX(u.created_at) AS last_at
        FROM llm_usage u WHERE u.plan_node_id IS NOT NULL AND ({where})
        GROUP BY u.plan_node_id,group_key,{','.join(_CLASSES)}"""


@cache
def definition() -> str:
    """Names the classification and grouping the triggers and rows were built with."""
    text = "|".join((*trigger_sql().values(), _grouped_select("1"), *NODE_USAGE_TABLES.values()))
    return hashlib.sha256(text.encode()).hexdigest()[:16]


@dataclass(frozen=True)
class Coverage:
    """Which nodes the stored groups answer for under the current definition."""

    complete: bool
    rebuilt_below: str

    def covers(self, node_id: str) -> bool:
        return self.complete or str(node_id) < self.rebuilt_below

    def params(self) -> dict[str, Any]:
        """Binds for a SQL reader's ``(:groups_complete=1 OR node_id<:groups_below)``."""
        return {"groups_complete": int(self.complete), "groups_below": self.rebuilt_below}


NO_COVERAGE = Coverage(False, "")


def ready(db) -> Coverage:
    """The nodes a reader may take from the stored groups; ``NO_COVERAGE`` until a rebuild under this definition."""
    try:
        row = db.execute("SELECT definition,rebuilt_below,complete FROM llm_usage_node_state WHERE id=1").fetchone()
    except Exception as error:  # a database without the table (older fixture) reads the ledger
        if "no such table" in str(error):
            return NO_COVERAGE
        raise
    if row is None or row[0] != definition():
        return NO_COVERAGE
    return Coverage(bool(row[2]), str(row[1]))


def _installed(db) -> dict[str, str]:
    marks = ",".join("?" * len(TRIGGERS))
    return {str(row[0]): str(row[1]) for row in db.execute(
        f"SELECT name,sql FROM sqlite_master WHERE type='trigger' AND name IN ({marks})", TRIGGERS)}


def _adopt(db) -> None:
    """Replace the triggers and restart coverage under the current definition, in one short transaction."""
    from infra.database import write_transaction

    with write_transaction(db, scope="node_usage_adopt"):
        for name in TRIGGERS:
            db.execute(f"DROP TRIGGER IF EXISTS {name}")
        for sql in trigger_sql().values():
            db.execute(sql)
        db.execute(
            "INSERT INTO llm_usage_node_state(id,definition,rebuilt_below,complete) VALUES (1,?,'',0) "
            "ON CONFLICT(id) DO UPDATE SET definition=excluded.definition,rebuilt_below='',complete=0",
            (definition(),),
        )


def _next_bound(db, low: str) -> str | None:
    """The exclusive upper node id of the batch starting at ``low``; None when it runs to the end."""
    row = db.execute(
        "SELECT plan_node_id FROM llm_usage INDEXED BY idx_llm_usage_plan_node "
        "WHERE plan_node_id IS NOT NULL AND plan_node_id>=? ORDER BY plan_node_id LIMIT 1 OFFSET ?",
        (low, REBUILD_ROWS),
    ).fetchone()
    if row is None:
        return None
    high = str(row[0])
    if high > low:
        return high
    # One node holds the whole batch: it is a batch of its own.
    row = db.execute(
        "SELECT MIN(plan_node_id) FROM llm_usage INDEXED BY idx_llm_usage_plan_node WHERE plan_node_id>?", (low,),
    ).fetchone()
    return None if row is None or row[0] is None else str(row[0])


def _rebuild_batch(db, low: str, high: str | None) -> None:
    from infra.database import write_transaction

    columns = ",".join(_COLUMNS)
    upper = "" if high is None else " AND plan_node_id<:high"
    with write_transaction(db, scope="node_usage_rebuild"):
        state = db.execute("SELECT definition,rebuilt_below,complete FROM llm_usage_node_state WHERE id=1").fetchone()
        if state is None or state[0] != definition() or state[2] or state[1] != low:
            return  # another start rebuilt this range
        bounds = {"low": low, "high": high}
        db.execute(f"DELETE FROM llm_usage_node_groups WHERE plan_node_id>=:low{upper}", bounds)
        db.execute(
            f"INSERT INTO llm_usage_node_groups({columns}) "
            + _grouped_select(f"u.plan_node_id>=:low{upper.replace('plan_node_id', 'u.plan_node_id')}"),
            bounds,
        )
        db.execute(
            "UPDATE llm_usage_node_state SET rebuilt_below=?,complete=? WHERE id=1",
            (high or "", int(high is None)),
        )


def ensure(db) -> dict[str, Any]:
    """Make the triggers and stored groups current before the server serves.

    Runs from ``init_db`` right after the schema migrations, never inside a request.
    When the definition or the installed triggers differ from this build's, the
    triggers are replaced and every node is rebuilt from the ledger, a ``REBUILD_ROWS``
    range per transaction; a start that stops part way resumes from ``rebuilt_below``.
    Readers take only covered nodes from the groups meanwhile (``ready``).
    """
    started = time.perf_counter()
    if db.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='llm_usage_node_state'").fetchone() is None:
        return {"rebuilt": False, "reason": "no_table"}
    state = db.execute("SELECT definition,rebuilt_below,complete FROM llm_usage_node_state WHERE id=1").fetchone()
    if _installed(db) != trigger_sql() or state is None or state[0] != definition():
        _adopt(db)
        state = (definition(), "", 0)
    if state[2]:
        return {"rebuilt": False, "reason": "current"}
    batches, longest = 0, 0.0
    low = str(state[1])
    while True:
        high = _next_bound(db, low)
        begun = time.perf_counter()
        _rebuild_batch(db, low, high)
        longest = max(longest, time.perf_counter() - begun)
        batches += 1
        if high is None:
            break
        low = high
    result = {"rebuilt": True, "batches": batches, "longest_batch_s": round(longest, 3),
              "wall_s": round(time.perf_counter() - started, 3)}
    log.info("Rebuilt plan-node usage groups: %s", result)
    return result


def recount(db, node_ids: list[str] | None = None) -> list[dict[str, Any]]:
    """Every difference between the stored groups and the ledger; empty when they agree.

    The ledger side is Measurement's own grouped read (``_shapes_sql``) keyed by node,
    commitment and Turn, plus the token totals and latest call time read directly, so
    it shares no code with the triggers or the rebuild. ``last_at`` must be at least
    the latest call; every other column must be equal.
    """
    from core.measurement_pricing import _shapes_sql

    where = "u.plan_node_id IS NOT NULL"
    params: tuple = ()
    if node_ids is not None:
        where += " AND u.plan_node_id IN (SELECT value FROM json_each(?))"
        import json
        params = (json.dumps(sorted(node_ids)),)
    # ``_shapes_sql`` already groups on model and manifest; only the plan keys lead.
    leading = {name: f"u.{name}" for name in _KEY[:3]}
    shapes = _shapes_sql(f"llm_usage u WHERE {where}", leading)
    key = (*_KEY, *_CLASSES)
    expected: dict[tuple, dict[str, Any]] = {}
    for row in db.execute(shapes, params).fetchall():
        row = dict(row)
        ok = row["admission"] == "ok"
        expected[tuple(row[name] for name in key)] = {
            "records": row["records"], **{bucket: (row[bucket] if ok else 0) for bucket in TOKEN_BUCKETS},
        }
    totals = db.execute(
        f"""SELECT {','.join(f'u.{name}' for name in _KEY)},
              admission,input_tier,idle_hours,
              SUM(CASE WHEN typeof(u.total_tokens)='integer' THEN u.total_tokens ELSE 0 END) AS total_tokens,
              COUNT(CASE WHEN typeof(u.total_tokens)='integer' THEN 1 END) AS total_known,
              COUNT(CASE WHEN u.total_tokens IS NOT NULL AND typeof(u.total_tokens)<>'integer' THEN 1 END) AS total_odd,
              MAX(u.created_at) AS latest
            FROM (SELECT u.*,{admission_sql('u')} AS admission,{input_tier_sql('u')} AS input_tier,
                    {idle_hours_sql('u')} AS idle_hours FROM llm_usage u WHERE {where}) u
            GROUP BY {','.join(f'u.{name}' for name in _KEY)},admission,input_tier,idle_hours""",
        params,
    ).fetchall()
    for row in totals:
        row = dict(row)
        expected[tuple(row[name] for name in key)].update(
            {name: row[name] for name in ("total_tokens", "total_known", "total_odd", "latest")})
    stored_where = "1" if node_ids is None else "plan_node_id IN (SELECT value FROM json_each(?))"
    stored = {
        tuple(row[name] for name in key): dict(row)
        for row in (dict(item) for item in db.execute(
            f"SELECT * FROM {NODE_GROUPS_TABLE} WHERE {stored_where}", params).fetchall())
    }
    differences: list[dict[str, Any]] = []
    for group in sorted(set(expected) | set(stored), key=repr):
        want, have = expected.get(group), stored.get(group)
        if want is None or have is None:
            differences.append({"group": group, "expected": want, "stored": have})
            continue
        wrong = {name: (want[name], have[name]) for name in ("records", *_SUMS) if want[name] != have[name]}
        if have["last_at"] < want["latest"]:
            wrong["last_at"] = (want["latest"], have["last_at"])
        if wrong:
            differences.append({"group": group, "differs": wrong})
    return differences
