"""Deactivating an agent stops its existing automation once."""

import pytest

from automation import schedules
from core.agent_roster_mutations import change_agent
from infra.database import get_db
from tests.test_script_schedules import _register


@pytest.mark.parametrize("inactive_first", [False, True])
def test_deactivate_stops_all_kinds_and_rules_but_status_and_reenable_do_not(isolated_db, tmp_path, inactive_first):
    db = get_db()
    for thread in ("owner", "other"):
        db.execute("INSERT INTO threads (id,user_id,title,agent_enabled,agent_responsibility,agent_status,created_at,updated_at) "
                   "VALUES (?,'user',?,1,'owns work','active','t','t')", (thread, thread))
    t0 = "2026-01-01T00:00:00+00:00"
    clock = schedules.create("owner", "__workspace__", "owner", kind="clock", spec="2026-01-01T00:01:00+00:00", max_runs=1, now=t0, db=db)
    cadence = schedules.create("owner", "__workspace__", "owner", kind="cadence", spec="60", max_runs=2, now=t0, db=db)
    script = _register(db, tmp_path, now=t0)
    other = schedules.create("other", "__workspace__", "other", kind="cadence", spec="60", max_runs=2, now=t0, db=db)
    for rule_id, owner in (("r1", "owner"), ("r2", "other")):
        db.execute('INSERT INTO event_rules (id,created_at,updated_at,scope,connector,event_type,'
                   'action,"to",mode,origin,created_by,authority_ref) '
                   "VALUES (?,'t','t','project:p','x','y','deliver',?,'wake','x',?,'x')",
                   (rule_id, 'thread:' + owner, owner))
    db.commit()
    db.close()
    if inactive_first:
        change_agent("owner", {"active": False}, user_id="user")
    db = get_db()
    assert all(schedules.get(x.id, db=db).state == "active" for x in (clock, cadence, script, other))
    assert db.execute("SELECT count(*) FROM event_rules WHERE enabled=1").fetchone()[0] == 2
    before = db.execute("SELECT count(*) FROM context_change_journal WHERE object_kind='agent_roster'").fetchone()[0]
    db.close()
    change_agent("owner", {"agent_enabled": False}, user_id="user")
    db = get_db()
    assert all(schedules.get(x.id, db=db).state == "cancelled" for x in (clock, cadence, script))
    assert schedules.get(other.id, db=db).state == "active"
    assert [tuple(row) for row in db.execute("SELECT id,enabled FROM event_rules ORDER BY id")] == [("r1", 0), ("r2", 1)]
    entries = db.execute("SELECT summary FROM context_change_journal WHERE object_kind='agent_roster' ORDER BY seq").fetchall()
    if inactive_first:
        assert len(entries) == before
    else:
        assert len(entries) == before + 1
        assert "stopped 3 schedules and 1 event rules" in entries[-1][0]
    db.close()
    change_agent("owner", {"agent_enabled": True}, user_id="user")
    db = get_db()
    assert schedules.get(clock.id, db=db).state == "cancelled"
    assert db.execute("SELECT enabled FROM event_rules WHERE id='r1'").fetchone()[0] == 0
    db.close()
