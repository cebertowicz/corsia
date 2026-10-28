"""Creator lifecycle guards apply before any rule action is queued."""

from events import rule_reads, rules
from events.rule_outcomes import Rule, RuleEvent, RulePlan
from tests.test_continuation_schedules import _db


def test_creator_archive_holds_then_resumes_and_purge_disables_only_its_rule():
    db = _db()
    db.execute("INSERT INTO projects (id,name,created_at,updated_at) VALUES ('p','p','t','t')")
    db.execute("UPDATE threads SET project_id='p' WHERE id='thread'")
    db.execute("INSERT INTO threads (id,user_id,project_id,created_at,updated_at) "
               "VALUES ('other','user','p','t','t')")
    for rule_id, owner in (("r1", "thread"), ("r2", "other")):
        db.execute('INSERT INTO event_rules (id,created_at,updated_at,scope,connector,event_type,'
                   'action,"to",mode,origin,created_by,authority_ref) '
                   "VALUES (?,?,?,'project:p','x','y','deliver',?,'wake','x',?,'x')",
                   (rule_id, '2026-01-01', '2026-01-01', 'thread:' + owner, owner))
    event = RuleEvent("e", "x", "y", "x.y", {}, None)
    rule = Rule("r1", "project:p", "x", "y", (), "deliver", "thread:thread", "wake", None, "thread")
    recipients = {"r1": "thread"}

    def decide():
        plan = RulePlan(event)
        threads = rule_reads.threads(db, ["thread"])
        remaining = rules._settle_unreachable(db, plan, [rule], recipients, threads)
        return plan, remaining

    db.execute("UPDATE threads SET lifecycle_state='archived', archived_reason='user' WHERE id='thread'")
    held, remaining = decide()
    assert not remaining and [(x.outcome, x.reason) for x in held.settled] == [("held", "creator unavailable")]
    assert not held.adds and not held.rides
    db.execute("UPDATE threads SET lifecycle_state='active', archived_reason=NULL WHERE id='thread'")
    resumed, remaining = decide()
    assert remaining == [rule] and not resumed.settled
    db.execute("UPDATE threads SET lifecycle_state='purged' WHERE id='thread'")
    gone, remaining = decide()
    assert not remaining and [(x.outcome, x.reason) for x in gone.settled] == [("dead_lettered", "creator gone")]
    assert [tuple(row) for row in db.execute("SELECT id,enabled FROM event_rules WHERE id IN ('r1','r2') ORDER BY id")] == [("r1", 0), ("r2", 1)]
