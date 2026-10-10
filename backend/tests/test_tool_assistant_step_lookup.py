"""Assistant Step binding must not scan a long thread while holding the writer."""

from __future__ import annotations

import json
import sqlite3


def test_assistant_step_lookup_uses_exact_step_index(isolated_db):
    from core._tool_result_delivery_support import select_provider_call

    db = sqlite3.connect(isolated_db)
    db.row_factory = sqlite3.Row
    db.execute(
        "INSERT INTO threads (id,user_id,model,project_id,created_at,updated_at) "
        "VALUES (?,?,?,?,?,?)",
        ("long-thread", "test-user", "test/model", "test-project", "t", "t"),
    )
    db.executemany(
        "INSERT INTO messages (id,thread_id,role,content,metadata,created_at,seq) "
        "VALUES (?,?,?,?,?,?,?)",
        (
            (f"other-{i}", "long-thread", "assistant", "",
             json.dumps({"turn_id": f"other-turn-{i}"}), "t", i)
            for i in range(4000)
        ),
    )
    db.execute(
        "INSERT INTO messages (id,thread_id,role,content,metadata,created_at,seq) "
        "VALUES (?,?,?,?,?,?,?)",
        (
            "target-step", "long-thread", "assistant", "",
            json.dumps({
                "turn_id": "target-turn",
                "llm_tool_calls": [{
                    "id": "call-1", "type": "function",
                    "function": {"name": "read_file", "arguments": "{}"},
                }],
            }),
            "t", 4000,
        ),
    )
    db.commit()

    progress_calls = 0

    def stop_full_scan() -> int:
        nonlocal progress_calls
        progress_calls += 1
        return int(progress_calls > 5)

    db.set_progress_handler(stop_full_scan, 1000)
    try:
        assert select_provider_call(
            db=db,
            thread_id="long-thread",
            assistant_step_id="target-step",
            provider_tool_call_id="call-1",
            name="read_file",
            arguments={},
        ) == ("call-1", {})
    finally:
        db.set_progress_handler(None, 0)
        db.close()
