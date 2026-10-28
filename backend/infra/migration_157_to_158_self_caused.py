"""Schema 157 -> 158: separate self-caused events and shell-only prefixes."""
from __future__ import annotations

import sqlite3

_MATCH_CHECKS = " AND ".join(
    f"(json_array_length(\"match\") <= {i} OR COALESCE(("
    f"json_type(\"match\", '$[{i}]') = 'object' AND "
    f"json_type(\"match\", '$[{i}].field') = 'text' AND "
    f"length(json_extract(\"match\", '$[{i}].field')) > 0 AND "
    f"json_extract(\"match\", '$[{i}].op') IN ('eq','not_eq','in','prefix','present') AND "
    f"(json_extract(\"match\", '$[{i}].op') != 'in' OR "
    f"json_type(\"match\", '$[{i}].value') = 'array') AND "
    f"(json_extract(\"match\", '$[{i}].op') != 'prefix' OR "
    f"json_type(\"match\", '$[{i}].value') = 'text') AND "
    f"(json_extract(\"match\", '$[{i}].op') = 'present' OR "
    f"json_type(\"match\", '$[{i}].value') IS NOT NULL)), 0))"
    for i in range(4)
)

_RULES = f"""CREATE TABLE IF NOT EXISTS event_rules (
        id TEXT PRIMARY KEY NOT NULL,
        created_at TEXT NOT NULL,
        updated_at TEXT NOT NULL,
        enabled INTEGER NOT NULL DEFAULT 1 CHECK (enabled IN (0, 1)),
        -- route: act on events. observe: register what a tool result names to its caller.
        kind TEXT NOT NULL DEFAULT 'route' CHECK (kind IN ('route', 'observe')),
        -- Only a shipped rule set's observe row applies to every project, as global.
        scope TEXT NOT NULL CHECK (scope LIKE 'project:_%'
                                   OR (scope = 'global' AND kind = 'observe' AND origin LIKE 'rule_set:_%')),
        connector TEXT NOT NULL,
        event_type TEXT,
        "match" TEXT NOT NULL DEFAULT '[]'
            CHECK (json_valid("match") AND json_type("match") = 'array'
                   AND json_array_length("match") <= 4)
            CHECK ({_MATCH_CHECKS}),
        action TEXT CHECK (action IN ('deliver', 'drop', 'run_script', 'start_worker')),
        "to" TEXT CHECK ("to" = 'registered_owner' OR "to" LIKE 'thread:_%'),
        mode TEXT CHECK (COALESCE((action = 'deliver' AND mode IN ('wake', 'digest'))
                                  OR (action != 'deliver' AND mode IS NULL)
                                  OR (kind = 'observe' AND mode IS NULL), 0)),
        note TEXT CHECK (note IS NULL OR (action = 'deliver' AND length(note) <= 500)),
        origin TEXT NOT NULL,
        created_by TEXT NOT NULL,
        authority_ref TEXT NOT NULL,
        -- run_script: the writer's own script schedule, which it runs with the event's facts.
        script_id TEXT CHECK (COALESCE(action = 'run_script', 0) = (script_id IS NOT NULL)),
        -- start_worker: the workflow its worker runs, and the event fact naming one request.
        workflow_id TEXT CHECK (COALESCE(action = 'start_worker', 0) = (workflow_id IS NOT NULL)),
        request_identity_fact TEXT
            CHECK (COALESCE(action = 'start_worker', 0) = (request_identity_fact IS NOT NULL)),
        -- observe: the tool, shell command's leading arguments, and artifact type.
        tool TEXT,
        argv_prefix TEXT CHECK (argv_prefix IS NULL OR (json_valid(argv_prefix)
                                AND json_type(argv_prefix) = 'array'
                                AND json_array_length(argv_prefix) > 0)),
        item_type TEXT,
        -- A route rule names its event type, action and recipient and an observe rule names its tool
        -- and artifact type, never both. (No semicolon in these comments: migration 139 runs this
        -- text statement by statement.)
        CHECK ((kind = 'route' AND event_type IS NOT NULL AND action IS NOT NULL AND "to" IS NOT NULL
                AND tool IS NULL AND argv_prefix IS NULL AND item_type IS NULL)
            OR (kind = 'observe' AND tool IS NOT NULL AND item_type IS NOT NULL
                AND ((tool = 'bash' AND argv_prefix IS NOT NULL) OR (tool != 'bash' AND argv_prefix IS NULL))
                AND event_type IS NULL AND action IS NULL AND "to" IS NULL AND mode IS NULL
                AND note IS NULL AND script_id IS NULL AND workflow_id IS NULL
                AND request_identity_fact IS NULL AND "match" = '[]')),
        -- A drop removes only its writer's own deliveries, and a script run or worker is the writer's own.
        CHECK (kind = 'observe' OR action = 'deliver' OR "to" = 'thread:' || created_by)
    )"""


def migrate_157_to_158(conn: sqlite3.Connection) -> None:
    conn.execute("ALTER TABLE event_rules RENAME TO event_rules_157")
    conn.execute(_RULES)
    columns = [row[1] for row in conn.execute("PRAGMA table_info(event_rules)")]
    names = ",".join(f'"{name}"' for name in columns)
    conn.execute(f"INSERT INTO event_rules ({names}) SELECT {names} FROM event_rules_157")
    conn.execute("DROP TABLE event_rules_157")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_event_rules_type\n        ON event_rules(connector, event_type, enabled, scope, created_at)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_event_rules_scope\n        ON event_rules(scope, created_at)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_event_rules_observe\n        ON event_rules(tool, enabled, scope) WHERE kind = 'observe'")
    conn.execute("""CREATE TABLE IF NOT EXISTS self_caused_events (
        connector TEXT NOT NULL,
        event_key TEXT NOT NULL,
        thread_id TEXT NOT NULL,
        at TEXT NOT NULL,
        PRIMARY KEY (connector, event_key, thread_id)
    )""")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_self_caused_events_at ON self_caused_events(at)")
