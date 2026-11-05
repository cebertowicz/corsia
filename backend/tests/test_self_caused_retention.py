"""Marker retention is maintained by the marker writer."""
from events.self_caused import record_result
from infra.database import get_db, write_transaction
from tests.test_event_rules_2a import _agent, _project
from tools.mcp_stamp import ConnectedAppResult


def test_record_result_prunes_expired_markers_without_rule_outcomes(isolated_db):
    _project("proj-a")
    _agent("owner")
    with write_transaction(None, scope="test") as db:
        db.execute(
            "INSERT INTO self_caused_events (connector,event_key,thread_id,at) VALUES (?,?,?,?)",
            ("github", "old", "owner", "2000-01-01T00:00:00+00:00"),
        )
    comment = ConnectedAppResult(
        '{"id":"123","url":"https://github.com/o/r/pull/7#issuecomment-123"}',
        {"mcp_app": "https://api.githubcopilot.com", "mcp_tool": "add_issue_comment"},
    )
    record_result("owner", comment)
    db = get_db()
    try:
        keys = [row[0] for row in db.execute("SELECT event_key FROM self_caused_events")]
        outcomes = db.execute("SELECT count(*) FROM event_route_decisions").fetchone()[0]
    finally:
        db.close()
    assert "old" not in keys
    assert len(keys) == 1
    assert outcomes == 0
