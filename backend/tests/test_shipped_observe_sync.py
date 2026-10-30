from dataclasses import replace
import json
import sqlite3

from events import connectors, observe_rules
from infra.event_rule_schema import EVENT_RULE_SCHEMA_SQL


def test_sync_reconciles_manifest_declarations_and_preserves_agent_rows(monkeypatch):
    db = sqlite3.connect(":memory:")
    db.row_factory = sqlite3.Row
    db.executescript(EVENT_RULE_SCHEMA_SQL)

    observe_rules.sync_shipped_rules(db)
    declared = {r.id: (m.connector, r.tool,
                       list(r.argv_prefix) if r.tool == "bash" else None, r.item_type)
                for name in connectors.registered_connectors()
                for m in [connectors.get_manifest(name)] for r in m.shipped_observe_rules}
    rows = db.execute("SELECT * FROM event_rules").fetchall()
    assert {r['id']: (r['connector'], r['tool'], json.loads(r['argv_prefix']) if r['argv_prefix'] else None, r['item_type'])
            for r in rows} == declared

    original = connectors.get_manifest('github')
    declaration = original.shipped_observe_rules[0]
    changed = replace(declaration, argv_prefix=('gh', 'pr', 'view'))
    monkeypatch.setitem(connectors._REGISTRY, 'github', replace(original, version='2', shipped_observe_rules=(changed,)))
    db.execute("UPDATE event_rules SET enabled=0 WHERE id=?", (declaration.id,))
    db.execute("INSERT INTO event_rules (id,created_at,updated_at,kind,scope,connector,origin,created_by,authority_ref,tool,argv_prefix,item_type) VALUES ('stale','a','a','observe','global','example','rule_set:example@1','rule_set:example@1','shipped:example@1','bash','[\"old\"]','thing')")
    db.execute("INSERT INTO event_rules (id,created_at,updated_at,kind,scope,connector,origin,created_by,authority_ref,tool,argv_prefix,item_type) VALUES ('agent','a','a','observe','project:p','github','agent:a','a','self','bash','[\"x\"]','pull-request')")
    db.execute("INSERT INTO event_route_decisions (at,event_id,connector,event_type,watch_event,rule_id,outcome,via) VALUES ('a','e','example','observe:bash','','stale','registered','rule')")
    db.commit()
    observe_rules.sync_shipped_rules(db)
    row = db.execute('SELECT * FROM event_rules WHERE id=?', (declaration.id,)).fetchone()
    assert (row['enabled'], row['origin'], json.loads(row['argv_prefix'])) == (0, 'rule_set:github@2', ['gh', 'pr', 'view'])
    assert db.execute("SELECT id FROM event_rules WHERE id='stale'").fetchone() is None
    assert db.execute("SELECT rule_id FROM event_route_decisions WHERE rule_id='stale'").fetchone() is not None
    assert db.execute("SELECT id FROM event_rules WHERE id='agent'").fetchone() is not None
    before = db.total_changes
    observe_rules.sync_shipped_rules(db)
    assert db.total_changes == before
