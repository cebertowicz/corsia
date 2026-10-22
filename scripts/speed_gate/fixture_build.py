"""Build the synthetic fixture database through the target tree's real schema.

Run in a subprocess whose environment points at disposable roots:

    python -m speed_gate.fixture_build --backend <dir> --out <file.json manifest>

The schema (tables, indexes, triggers, ``user_version``) comes from the target
tree's ``init_db``; this module only fills it. Core tables are hand-shaped to
the census skew; the remaining tables with a census figure are filled by
``fixture_rows``. A table whose constraints reject a generated row is recorded
in the manifest's ``unseeded`` list. Every table the build leaves empty, because the census has no figure
for it or its rows were rejected, must be listed with a reason in ``fixture_excluded``; one that is not
fails the build (``FixtureCoverageError``), and the listed ones are reported in the manifest. Likewise every
table's stored rows are compared with its census target: one below it fails unless ``fixture_excluded.SHORT``
lists it with its exact count and reason, and the listed shortfalls are reported in the manifest too.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import random
import sys
import time
from pathlib import Path

from .sqlite_pin import sqlite3
from . import fixture_core as core
from . import fixture_excluded as excluded
from . import fixture_hosts as hosts
from . import fixture_plans as plans
from . import fixture_presence as presence
from . import fixture_rows as rows
from . import fixture_spec as spec

#: Tables built by hand; the generic pass skips them.
HAND_BUILT = frozenset({"projects", "threads", "messages", "llm_usage", "plans", "plan_nodes",
                        "plan_node_attempts", "plan_node_events",
                        "presence_visits", "thread_presence"})
#: Generic tables whose rows other generic tables reference, in dependency order.
FIRST = ("artifact_identities", "active_turns", "checkpoints")


#: Tables with a partial unique index ("at most one live row"): the generic pass would
#: make every row live, so these columns are pinned to a terminal value.
TERMINAL = {
    "active_turns": {"status": "completed"},
    "checkpoints": {"status": "installed"},
    "host_access_leases": {"state": "expired"},
    "plan_node_holds": {"kind": "blocked"},
}


class FixtureCoverageError(RuntimeError):
    """A table is empty in the fixture and nothing says why."""


def _overrides(table: str) -> dict:
    found = {name: (lambda i, r, p, v=value: v) for name, value in TERMINAL.get(table, {}).items()}
    if table == "plan_node_dependencies":
        found["depends_on_node_id"] = lambda i, r, p: p.ids["node"][(i * 13 + 5) % len(p.ids["node"])]
        found["node_id"] = lambda i, r, p: p.ids["node"][(i * 7 + 1) % len(p.ids["node"])]
    if table == "automation_schedules":
        # Nullable only for a script started solely by event rules; a clock or cadence row needs it.
        found["next_run_at"] = lambda i, r, p: rows.timestamp(r)
    return found


def _seed_generic(db: sqlite3.Connection, rng: random.Random, pools: rows.Pools,
                  manifest: dict) -> None:
    tables = [r[0] for r in db.execute(
        "SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%'"
        " AND sql NOT LIKE 'CREATE VIRTUAL%' ORDER BY name")]
    ordered = [t for t in FIRST if t in tables] + [t for t in tables if t not in FIRST]
    for table in ordered:
        want = spec.target_rows(table)
        if want is None or table in HAND_BUILT:
            if want is None and table not in HAND_BUILT:
                manifest["no_target"].append(table)
            continue
        have = db.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
        need = max(0, want - have)
        try:
            db.execute("SAVEPOINT seed_table")
            stored = rows.insert_batched(
                db, table, rows.generate_rows(db, table, need, rng, pools, _overrides(table)))
            db.execute("RELEASE seed_table")
        except sqlite3.Error as error:
            db.execute("ROLLBACK TO seed_table")
            db.execute("RELEASE seed_table")
            manifest["unseeded"].append({"table": table, "reason": f"{type(error).__name__}: {error}"})
            continue
        manifest["tables"][table] = {"target": want, "rows": have + stored}
        if table == "artifact_identities":
            pools.add("artifact", [r[0] for r in db.execute("SELECT id FROM artifact_identities")])


def _account_for_empty_tables(manifest: dict) -> None:
    """Fail on a table left empty without a listed reason; record the listed ones in the manifest."""
    empty = sorted({*manifest["no_target"], *(item["table"] for item in manifest["unseeded"])})
    unexplained = [table for table in empty if table not in excluded.EXCLUDED]
    if unexplained:
        rejected = {item["table"]: item["reason"] for item in manifest["unseeded"]}
        raise FixtureCoverageError(
            "table(s) empty in the fixture with no listed reason: "
            + "; ".join(f"{table} ({rejected.get(table, 'no census figure in fixture_spec')})" for table in unexplained)
            + ". Give each a census figure in fixture_spec, or list it with a reason in fixture_excluded.")
    manifest["excluded"] = {table: excluded.EXCLUDED[table] for table in empty}


def _check_row_targets(db: sqlite3.Connection, manifest: dict) -> None:
    """Every table with a census target holds at least that many rows, or is listed in ``fixture_excluded.SHORT``.

    Counted from the stored rows, not from what the build meant to insert. A listed table must hold exactly the
    listed count, so a fixed or changed shortfall makes the entry stale and fails the build. Shortfalls are
    carried into the manifest (``short``) and from there into the report.
    """
    short: dict[str, dict] = {}
    problems: list[str] = []
    tables = [r[0] for r in db.execute(
        "SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%'"
        " AND sql NOT LIKE 'CREATE VIRTUAL%' ORDER BY name")]
    for table in tables:
        want = spec.target_rows(table)
        if want is None:
            continue
        have = db.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
        listed = excluded.SHORT.get(table)
        if have >= want:
            if listed:
                problems.append(f"{table} has {have} row(s), at its target of {want}, but is listed as short at {listed[0]}")
        elif listed is None or listed[0] != have:
            problems.append(f"{table} has {have} row(s), target {want}, "
                            + (f"listed as {listed[0]}" if listed else "not listed"))
        else:
            short[table] = {"target": want, "rows": have, "reason": listed[1]}
    problems += [f"{table} is listed as short but has no census target in this schema"
                 for table in sorted(excluded.SHORT) if table not in tables or spec.target_rows(table) is None]
    if problems:
        raise FixtureCoverageError(
            "table row counts disagree with the census targets: " + "; ".join(problems)
            + ". Make the build reach the target, or list the table with its actual count and reason "
            "in fixture_excluded.SHORT.")
    manifest["short"] = short


def build_into(db: sqlite3.Connection) -> dict:
    """Fill an initialised database; returns the build manifest."""
    rng = random.Random(spec.SEED)
    pools = rows.Pools(rng)
    manifest: dict = {"seed": spec.SEED, "scale": spec.SCALE, "tables": {}, "unseeded": [],
                      "no_target": []}
    db.execute("PRAGMA synchronous=OFF")
    db.execute("PRAGMA journal_mode=MEMORY")
    db.execute("PRAGMA cache_size=-262144")
    hosts.pin_cli_connections(db)
    message_counts, usage_counts = core.seed_counts(rng)
    core.seed_projects(db, rng, pools)
    core.seed_threads(db, rng, pools, message_counts)
    manifest["tables"]["messages"] = {"target": spec.target_rows("messages"),
                                      "rows": core.seed_messages(db, rng, message_counts)}
    manifest["tables"]["llm_usage"] = {"target": spec.target_rows("llm_usage"),
                                       "rows": core.seed_usage(db, rng, usage_counts)}
    pools.add("vendor_account", [r[0] for r in db.execute("SELECT id FROM vendor_accounts")])
    plans.seed_plans(db, rng, pools)
    manifest["tables"]["plan_nodes"] = {"target": spec.target_rows("plan_nodes"),
                                        "rows": plans.seed_nodes(db, rng, pools)}
    manifest["tables"]["plan_node_attempts"] = {"target": spec.target_rows("plan_node_attempts"),
                                                "rows": plans.seed_attempts(db, rng, pools)}
    plans.attribute_usage(db)
    manifest["tables"]["plan_node_events"] = {"target": spec.target_rows("plan_node_events"),
                                              "rows": plans.seed_node_events(db, rng)}
    manifest["tables"]["presence_visits"] = {"target": spec.target_rows("presence_visits"),
                                             "rows": presence.seed_visits(db, rng)}
    manifest["tables"]["thread_presence"] = {"target": spec.target_rows("thread_presence"),
                                             "rows": presence.seed_open_visits(db, rng)}
    _seed_generic(db, rng, pools, manifest)
    _account_for_empty_tables(manifest)
    _check_row_targets(db, manifest)
    db.commit()
    manifest["top_thread"] = {"id": "thread-00000", "messages": message_counts[0],
                              "llm_usage": usage_counts[0]}
    return manifest


def source_hash() -> str:
    """Digest of the fixture builder's own source, recorded in the manifest so a measurement names the exact
    fixture code it ran on. Nothing is cached or keyed on it: every run rebuilds the fixture."""
    digest = hashlib.sha256()
    for path in sorted(Path(__file__).parent.glob("fixture*.py")):
        digest.update(path.read_bytes())
    return digest.hexdigest()


def schema_digest(db: sqlite3.Connection) -> dict:
    """The real schema's identity: ``user_version`` and a hash of every DDL statement."""
    ddl = "\n".join(r[0] for r in db.execute(
        "SELECT sql FROM sqlite_master WHERE sql IS NOT NULL ORDER BY type, name"))
    return {"user_version": db.execute("PRAGMA user_version").fetchone()[0],
            "ddl_sha256": hashlib.sha256(ddl.encode()).hexdigest()}


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--backend", required=True, type=Path)
    parser.add_argument("--manifest", required=True, type=Path)
    args = parser.parse_args(argv)
    sys.path.insert(0, str(args.backend))
    os.environ.setdefault("LITELLM_LOCAL_MODEL_COST_MAP", "true")  # no network at import
    from infra.database import DB_PATH, close_thread_db, init_db  # late: reads CORSIA_* env

    started = time.monotonic()
    init_db()
    close_thread_db()
    db = sqlite3.connect(str(DB_PATH))
    try:
        manifest = build_into(db)
    except FixtureCoverageError as error:
        print(f"fixture build failed: {error}", file=sys.stderr)
        return 1
    manifest["schema"] = schema_digest(db)
    manifest["builder_sha256"] = source_hash()
    manifest["sqlite_version"] = sqlite3.sqlite_version
    db.execute("PRAGMA journal_mode=WAL")
    db.execute("PRAGMA wal_checkpoint(TRUNCATE)")
    db.close()
    manifest["db_path"] = str(DB_PATH)
    manifest["build_seconds"] = round(time.monotonic() - started, 1)  # informational only
    manifest["size_bytes"] = os.path.getsize(DB_PATH)
    args.manifest.write_text(json.dumps(manifest, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
