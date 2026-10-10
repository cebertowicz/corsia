"""The agent-grain attention time behind minutes per outcome on the Agents table."""

from __future__ import annotations

from datetime import datetime, timezone

import pytest

from api.measurement_agent_attention import query_root_active
from api.measurement_queries import prepare_outcome_periods
from core.measurement_scope import resolve_grain

LATEST, PREVIOUS = "2026-09-28", "2026-09-21"
SCOPE = {"scope_project": "all", "scope_agent": None, "user_id": "steve"}
GRAIN = resolve_grain("weeks", "UTC", now=datetime(2026, 10, 5, 12, tzinfo=timezone.utc))
PARAMS = {**SCOPE, "grain": GRAIN}


@pytest.fixture
def db(isolated_db):
    from infra.database import get_db

    connection = get_db()
    for thread in ("root", "other"):
        connection.execute("INSERT INTO threads (id,user_id,title,agent_enabled,agent_status,created_at,updated_at) VALUES (?,'steve',?,1,'active','t','t')", (thread, thread))
    try:
        yield connection
    finally:
        connection.close()


def test_attention_time_is_counted_inside_each_agent_not_across_them(db):
    def message(message_id: str, thread: str, at: str) -> None:
        db.execute("INSERT INTO messages (id,thread_id,role,content,metadata,created_at) VALUES (?,?, 'user', 'x', '{}', ?)",
                   (message_id, thread, at))

    message("a1", "root", "2026-09-29T12:00:00Z")
    message("o1", "other", "2026-09-29T12:02:00Z")  # lands between root's two messages
    message("a2", "root", "2026-09-29T12:05:00Z")
    params = {**PARAMS}
    prepare_outcome_periods(db, params, roots=True)
    rows = {row["root_id"]: row for row in query_root_active(db, params)}
    # root: 60 s for the first message and 300 s gap + 60 s for the second; other: 60 s alone.
    assert rows["root"]["period"] == LATEST and rows["root"]["active_hours"] == pytest.approx(420 / 3600)
    assert rows["other"]["active_hours"] == pytest.approx(60 / 3600)
