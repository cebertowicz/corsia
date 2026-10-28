"""Schema 161 preserves rules while separating self-caused event identities."""
import sqlite3

import pytest

from infra.event_rule_schema import EVENT_RULE_SCHEMA_SQL
from infra.database_migrations import migrate_to_current
from infra.schema import SCHEMA_SQL, SCHEMA_VERSION


def test_160_rules_survive_and_schema_matches_fresh():
    old_sql = EVENT_RULE_SCHEMA_SQL.replace(
        "AND tool IS NOT NULL AND item_type IS NOT NULL\n"
        "                AND ((tool = 'bash' AND argv_prefix IS NOT NULL) OR (tool != 'bash' AND argv_prefix IS NULL))",
        "AND tool IS NOT NULL AND argv_prefix IS NOT NULL AND item_type IS NOT NULL",
    ).replace(
        "-- observe: the tool, shell command's leading arguments, and artifact type.",
        "-- observe: the tool whose result is read, the command's leading arguments (a shell only)\n"
        "        -- and the connector's artifact type its extractor reads from the result.",
    )
    db = sqlite3.connect(":memory:")
    old_sql = (
        old_sql[:old_sql.index("    CREATE TABLE IF NOT EXISTS self_caused_events (")]
        + old_sql[old_sql.index("    CREATE TABLE IF NOT EXISTS event_route_decisions ("):]
    )
    db.executescript(SCHEMA_SQL.replace(EVENT_RULE_SCHEMA_SQL, old_sql))
    db.execute("PRAGMA user_version=160")
    insert = ("INSERT INTO event_rules (id,created_at,updated_at,kind,scope,connector,origin,created_by,"
              "authority_ref,tool,argv_prefix,item_type) VALUES (?, 'a','a','observe','project:p','github',"
              "'agent:a','a','self',?,?, 'pull-request')")
    db.execute(insert, ("shell", "bash", '["gh"]'))
    db.commit()
    result = migrate_to_current(db, SCHEMA_VERSION)
    assert (result.from_version, result.to_version) == (160, 161)
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
