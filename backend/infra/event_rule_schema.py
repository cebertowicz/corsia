"""Agent-written event rules and the record of what each one did.

Shared by fresh installs and migrations 136, 138 and 141. ``event_rules`` holds only
rules an agent wrote through the ``event_rules`` tool: no row is seeded, and
today's routing stays in code. ``event_route_decisions`` is the one outcome
record for those rules; the tool's list and explain read it, a ``run_script``
rule's row is also its run's queue until the script
runner settles it, and a ``start_worker`` rule's row is its start's queue, its
request identity and its worker until the worker's result settles it.
"""
from __future__ import annotations

# Fixed-size checks keep a malformed test out of storage: at most four AND-ed
# tests, each naming a field and one of the five operators. These literals copy
# events.rules on purpose: migration 136's DDL must not move when that code does,
# and test_event_rule_schema_matches_rules fails when the two disagree.
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

EVENT_RULE_SCHEMA_SQL = f"""
    CREATE TABLE IF NOT EXISTS event_rules (
        id TEXT PRIMARY KEY NOT NULL,
        created_at TEXT NOT NULL,
        updated_at TEXT NOT NULL,
        enabled INTEGER NOT NULL DEFAULT 1 CHECK (enabled IN (0, 1)),
        scope TEXT NOT NULL CHECK (scope LIKE 'project:_%'),
        connector TEXT NOT NULL,
        event_type TEXT NOT NULL,
        "match" TEXT NOT NULL DEFAULT '[]'
            CHECK (json_valid("match") AND json_type("match") = 'array'
                   AND json_array_length("match") <= 4)
            CHECK ({_MATCH_CHECKS}),
        action TEXT NOT NULL CHECK (action IN ('deliver', 'drop', 'run_script', 'start_worker')),
        "to" TEXT NOT NULL CHECK ("to" = 'registered_owner' OR "to" LIKE 'thread:_%'),
        mode TEXT CHECK (COALESCE((action = 'deliver' AND mode IN ('wake', 'digest'))
                                  OR (action != 'deliver' AND mode IS NULL), 0)),
        note TEXT CHECK (note IS NULL OR (action = 'deliver' AND length(note) <= 500)),
        origin TEXT NOT NULL,
        created_by TEXT NOT NULL,
        authority_ref TEXT NOT NULL,
        -- run_script: the writer's own script schedule, which it runs with the event's facts.
        script_id TEXT CHECK ((action = 'run_script') = (script_id IS NOT NULL)),
        -- start_worker: the workflow its worker runs, and the event fact naming one request.
        workflow_id TEXT CHECK ((action = 'start_worker') = (workflow_id IS NOT NULL)),
        request_identity_fact TEXT
            CHECK ((action = 'start_worker') = (request_identity_fact IS NOT NULL)),
        -- A drop removes only its writer's own deliveries, and a script run or worker is the writer's own.
        -- (No semicolon in these comments: migration 139 runs this text statement by statement.)
        CHECK (action = 'deliver' OR "to" = 'thread:' || created_by)
    );
    CREATE INDEX IF NOT EXISTS idx_event_rules_type
        ON event_rules(connector, event_type, enabled, scope, created_at);
    CREATE INDEX IF NOT EXISTS idx_event_rules_scope
        ON event_rules(scope, created_at);

    CREATE TABLE IF NOT EXISTS event_route_decisions (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        at TEXT NOT NULL,
        event_id TEXT NOT NULL,
        connector TEXT NOT NULL,
        event_type TEXT NOT NULL,
        watch_event TEXT NOT NULL,
        artifact_integration TEXT,
        artifact_type TEXT,
        artifact_external_id TEXT,
        rule_id TEXT NOT NULL,
        thread_id TEXT,
        outcome TEXT NOT NULL CHECK (outcome IN (
            'woke', 'digested', 'dropped', 'held', 'dead_lettered', 'ended', 'duplicate',
            'queued', 'running', 'ran', 'failed', 'skipped',
            'started', 'returned', 'invalid', 'no_result', 'refused')),
        -- 'routed': the rule rode a delivery today's routing already made.
        via TEXT NOT NULL CHECK (via IN ('rule', 'routed')),
        reason TEXT,
        -- A queued run's input (the event and its rule chain), cleared when it is claimed. A
        -- start's input stays until its worker is made, then holds the worker's result check
        -- until the start settles.
        run_input TEXT CHECK (run_input IS NULL OR (outcome IN ('queued', 'started')
                                                    AND json_valid(run_input))),
        -- A worker start's request identity (R24) and, once started, its worker thread.
        request_identity TEXT,
        worker_thread_id TEXT
    );
    CREATE INDEX IF NOT EXISTS idx_event_route_decisions_rule
        ON event_route_decisions(rule_id, thread_id, outcome, at);
    CREATE INDEX IF NOT EXISTS idx_event_route_decisions_event
        ON event_route_decisions(event_id);
    CREATE INDEX IF NOT EXISTS idx_event_route_decisions_artifact
        ON event_route_decisions(artifact_external_id, at);
    CREATE INDEX IF NOT EXISTS idx_event_route_decisions_at
        ON event_route_decisions(at);
    -- The run queue in order (outcome = 'queued' ORDER BY id) and the runs in flight. Not partial:
    -- SQLite uses a partial index only when the query repeats its WHERE term, and these bind the outcome.
    CREATE INDEX IF NOT EXISTS idx_event_route_decisions_runs
        ON event_route_decisions(outcome, id);
    CREATE INDEX IF NOT EXISTS idx_event_route_decisions_request
        ON event_route_decisions(rule_id, request_identity);
    CREATE INDEX IF NOT EXISTS idx_event_route_decisions_worker
        ON event_route_decisions(worker_thread_id);
"""
