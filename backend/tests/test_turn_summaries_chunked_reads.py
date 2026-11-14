"""The Turn-summary read binds its Turn ids in chunks, not one per statement.

A page with no `limit` hands every row of the thread to the summary read, so the
number of Turn ids it receives is the thread's, not the page's.
"""

from __future__ import annotations

import importlib.util
import json
from pathlib import Path
import sqlite3

from infra.chat_comment_schema import CHAT_VISIBLE_CONTENT_SQL
from infra.id_bind_chunk import IDS_PER_STATEMENT

_module_path = Path(__file__).parents[1] / "api" / "thread_turn_summaries.py"
_spec = importlib.util.spec_from_file_location("_chunked_turn_summaries", _module_path)
assert _spec is not None and _spec.loader is not None
_thread_turn_summaries = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(_thread_turn_summaries)
page_turn_summaries = _thread_turn_summaries.page_turn_summaries

_SID = "sess-chunked"
_START, _END = "2026-06-12T00:00:00Z", "2026-06-12T00:04:00Z"


def test_more_turn_ids_than_one_statement_may_bind_still_get_summaries():
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    conn.execute(
        "CREATE TABLE messages (id TEXT PRIMARY KEY, thread_id TEXT, role TEXT, content TEXT, "
        "metadata TEXT, created_at TEXT, seq INTEGER, active_turn_id TEXT, "
        "delivery_state TEXT NOT NULL DEFAULT 'delivered', delivery_intent TEXT)"
    )
    conn.execute(
        "CREATE INDEX idx_messages_turn_calls ON messages(thread_id, role, "
        "json_extract(metadata, '$.turn_id')) WHERE role='assistant' AND json_valid(metadata)"
    )
    conn.execute(
        "CREATE TABLE active_turns (id TEXT, thread_id TEXT, status TEXT, started_at TEXT, "
        "ended_at TEXT, error TEXT)"
    )
    conn.executescript(CHAT_VISIBLE_CONTENT_SQL)
    # A lowered connection limit stands in for a thread whose Turn count is that
    # much larger than one statement may bind.
    conn.setlimit(sqlite3.SQLITE_LIMIT_VARIABLE_NUMBER, IDS_PER_STATEMENT + 10)
    turn_ids = [f"turn-{n}" for n in range(2 * IDS_PER_STATEMENT + 1)]
    for seq, turn_id in enumerate(turn_ids, start=1):
        conn.execute(
            "INSERT INTO active_turns VALUES (?,?,?,?,?,?)",
            (turn_id, _SID, "completed", _START, _END, None),
        )
        conn.execute(
            "INSERT INTO messages (id,thread_id,role,content,metadata,created_at,seq) "
            "VALUES (?,?,?,?,?,?,?)",
            (f"m-{turn_id}", _SID, "assistant", "done",
             json.dumps({"turn_id": turn_id}), f"2026-06-12T00:00:{seq:04d}Z", seq),
        )
    conn.commit()

    # What a no-`limit` page hands over: one display row per Turn of the thread.
    display = [
        {"id": f"m-{turn_id}", "metadata": {"turn_id": turn_id}} for turn_id in turn_ids
    ]
    summaries = page_turn_summaries(conn, _SID, display)

    assert sorted(summaries) == sorted(turn_ids)
    assert summaries[turn_ids[0]]["activity_count"] == 1
    assert summaries[turn_ids[-1]]["status"] == "completed"
