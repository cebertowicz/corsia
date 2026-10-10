"""Build the synthetic fixture database through the target tree's real schema.

Run in a subprocess whose environment points at disposable roots:

    python -m speed_gate.fixture_build --backend <dir> --out <file.json manifest>

The schema (tables, indexes, triggers, ``user_version``) comes from the target
tree's ``init_db``; this module only fills it. Core tables are hand-shaped to
the census skew; the remaining tables with a census figure are filled by
``fixture_rows``. A table whose constraints reject a generated row is recorded
in the manifest's ``unseeded`` list, never silently dropped and never fatal.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import random
import sqlite3
import sys
import time
from pathlib import Path

from . import fixture_core as core
from . import fixture_plans as plans
from . import fixture_rows as rows
from . import fixture_spec as spec

#: Tables built by hand; the generic pass skips them.
HAND_BUILT = frozenset({"projects", "threads", "messages", "llm_usage", "plans", "plan_nodes",
                        "plan_node_attempts", "plan_node_events"})
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


def _overrides(table: str) -> dict:
    found = {name: (lambda i, r, p, v=value: v) for name, value in TERMINAL.get(table, {}).items()}
    if table == "plan_node_dependencies":
        found["depends_on_node_id"] = lambda i, r, p: p.ids["node"][(i * 13 + 5) % len(p.ids["node"])]
        found["node_id"] = lambda i, r, p: p.ids["node"][(i * 7 + 1) % len(p.ids["node"])]
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


def build_into(db: sqlite3.Connection) -> dict:
    """Fill an initialised database; returns the build manifest."""
    rng = random.Random(spec.SEED)
    pools = rows.Pools(rng)
    manifest: dict = {"seed": spec.SEED, "scale": spec.SCALE, "tables": {}, "unseeded": [],
                      "no_target": []}
    db.execute("PRAGMA synchronous=OFF")
    db.execute("PRAGMA journal_mode=MEMORY")
    db.execute("PRAGMA cache_size=-262144")
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
    _seed_generic(db, rng, pools, manifest)
    db.commit()
    manifest["top_thread"] = {"id": "thread-00000", "messages": message_counts[0],
                              "llm_usage": usage_counts[0]}
    return manifest


def source_hash() -> str:
    """Digest of the fixture builder itself, part of the cache key."""
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
    manifest = build_into(db)
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
