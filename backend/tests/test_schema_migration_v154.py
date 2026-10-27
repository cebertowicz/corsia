"""Schema 154 preserves rules while separating self-caused event identities."""
import sqlite3

import pytest

from infra.event_rule_schema import EVENT_RULE_SCHEMA_SQL
from infra.migration_153_to_154_self_caused import migrate_153_to_154


def test_153_rules_survive_and_schema_matches_fresh():
    old_sql = EVENT_RULE_SCHEMA_SQL.replace(
        "AND ((tool = 'bash' AND argv_prefix IS NOT NULL) OR (tool != 'bash' AND argv_prefix IS NULL))",
        "AND argv_prefix IS NOT NULL",
    )
    db = sqlite3.connect(":memory:")
    db.executescript(old_sql)
    db.execute("DROP TABLE self_caused_events")
    insert = ("INSERT INTO event_rules (id,created_at,updated_at,kind,scope,connector,origin,created_by,"
              "authority_ref,tool,argv_prefix,item_type) VALUES (?, 'a','a','observe','project:p','github',"
              "'agent:a','a','self',?,?, 'pull-request')")
    db.execute(insert, ("shell", "bash", '["gh"]'))
    migrate_153_to_154(db)
    db.execute(insert, ("app", "create_pull_request", None))
    assert db.execute("SELECT id,tool,argv_prefix FROM event_rules ORDER BY id").fetchall() == [
        ("app", "create_pull_request", None), ("shell", "bash", '["gh"]')]
    with pytest.raises(sqlite3.IntegrityError):
        db.execute(insert, ("bad", "create_pull_request", '["gh"]'))
    db.execute("INSERT INTO self_caused_events VALUES ('github','issue_comment:1','a','2026-01-01')")
    fresh = sqlite3.connect(":memory:")
    fresh.executescript(EVENT_RULE_SCHEMA_SQL)
    query = ("SELECT type,name,sql FROM sqlite_master WHERE tbl_name IN "
             "('event_rules','event_route_decisions','self_caused_events') ORDER BY name")
    assert db.execute(query).fetchall() == fresh.execute(query).fetchall()
