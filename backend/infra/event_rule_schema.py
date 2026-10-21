"""The deliver/drop rule schema shared by fresh installs and migration 134."""
from __future__ import annotations

# Fixed-size checks keep malformed match objects out of storage, without a
# second runtime language or validation of actions that have no live consumer.
_MATCH_CHECKS = " AND ".join(
    f"(json_array_length(\"match\") <= {i} OR COALESCE(("
    f"json_type(\"match\", '$[{i}]') = 'object' AND "
    f"json_type(\"match\", '$[{i}].field') = 'text' AND "
    f"length(json_extract(\"match\", '$[{i}].field')) > 0 AND "
    f"json_extract(\"match\", '$[{i}].op') IN ('eq','present') AND "
    f"(json_extract(\"match\", '$[{i}].op') = 'present' OR "
    f"json_type(\"match\", '$[{i}].value') IS NOT NULL)), 0))"
    for i in range(4)
)

EVENT_RULE_SCHEMA_SQL = f"""
    CREATE TABLE IF NOT EXISTS event_rules (
        id TEXT PRIMARY KEY NOT NULL,
        created_at TEXT NOT NULL,
        updated_at TEXT NOT NULL,
        enabled INTEGER NOT NULL DEFAULT 1 CHECK(enabled IN (0,1)),
        scope TEXT NOT NULL DEFAULT 'global',
        connector TEXT NOT NULL,
        event_type TEXT NOT NULL DEFAULT '*',
        "match" TEXT NOT NULL CHECK(json_valid("match"))
            CHECK(json_type("match") = 'array' AND json_array_length("match") <= 4)
            CHECK({_MATCH_CHECKS}),
        "on" TEXT NOT NULL CHECK("on" IN ('first_seen','every')),
        action TEXT NOT NULL CHECK(action IN ('deliver','drop')),
        "to" TEXT,
        mode TEXT,
        precedence INTEGER NOT NULL DEFAULT 100,
        CHECK((action = 'drop' AND "to" IS NULL AND mode IS NULL) OR
              (action = 'deliver' AND "to" IS NOT NULL AND
               ("to" = 'registered_owner' OR
                ("to" LIKE 'thread:%' AND length("to") > 7)) AND
               mode IS NOT NULL AND mode IN ('wake','digest','default'))),
        CHECK("on" = 'every' OR (action = 'deliver' AND event_type = '*' AND
              "to" LIKE 'thread:%' AND "to" != 'thread:$corsia_tag' AND
              mode = 'default' AND json_array_length("match") = 1 AND
              json_extract("match", '$[0].op') = 'eq' AND
              json_type("match", '$[0].value') = 'text'))
    );
    CREATE INDEX IF NOT EXISTS idx_event_rules_match
        ON event_rules(connector, "on", enabled, event_type, scope, precedence, created_at);
"""

# These rows describe webhook behavior only. Poller events keep their current
# disposition. In particular tags override ownership, and all self-authored
# events are suppressed until the rule-authoring behavior change ships.
GITHUB_RULE_SEEDS = (
    ('rule_set:github@1:cancelled', 'webhook.github.check_suite_completed',
     '[{"field":"conclusion","op":"eq","value":"cancelled"}]', 'drop', None, None, 0),
    ('rule_set:github@1:self', '*',
     '[{"field":"webhook_self_authored","op":"eq","value":true}]', 'drop', None, None, 1),
    ('rule_set:github@1:tag', '*',
     '[{"field":"webhook","op":"eq","value":true},{"field":"corsia_tag","op":"present"}]',
     'deliver', 'thread:$corsia_tag', 'wake', 10),
    ('rule_set:github@1:owner', '*',
     '[{"field":"webhook","op":"eq","value":true},{"field":"corsia_tag","op":"eq","value":null}]',
     'deliver', 'registered_owner', 'wake', 100),
)


def _literal(value: str | int | None) -> str:
    if value is None:
        return 'NULL'
    if isinstance(value, int):
        return str(value)
    return "'" + value.replace("'", "''") + "'"


# Seeds belong to the schema itself: direct fresh-schema users (including
# fixtures) and both migration branches get exactly the same rule set.
for _id, _type, _match, _action, _to, _mode, _precedence in GITHUB_RULE_SEEDS:
    _values = (_id, '2026-10-21', '2026-10-21', 'github', _type, _match,
               'every', _action, _to, _mode, _precedence)
    EVENT_RULE_SCHEMA_SQL += (
        'INSERT OR IGNORE INTO event_rules '
        '(id,created_at,updated_at,connector,event_type,"match","on",action,"to",mode,precedence) VALUES ('
        + ','.join(_literal(value) for value in _values) + ');\n'
    )
