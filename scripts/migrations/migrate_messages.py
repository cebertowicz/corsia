#!/usr/bin/env python3
"""One-time migration: convert display-format messages to LLM format."""

from __future__ import annotations

import json
import sqlite3
import sys
import uuid
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent.parent / "backend"))
from infra.config import DB_PATH  # noqa: E402


def migrate():
    if not DB_PATH.exists():
        print(f"Database not found: {DB_PATH}")
        sys.exit(1)

    conn = sqlite3.connect(str(DB_PATH))
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")

    rows = conn.execute(
        "SELECT id, session_id, content, metadata, created_at FROM messages WHERE role = 'assistant' AND metadata IS NOT NULL ORDER BY created_at ASC"
    ).fetchall()

    migrated = 0
    tool_rows_inserted = 0

    for row in rows:
        msg_id = row["id"]
        session_id = row["session_id"]
        raw_meta = row["metadata"]
        created_at = row["created_at"]

        if not raw_meta:
            continue

        try:
            meta = json.loads(raw_meta)
        except (json.JSONDecodeError, TypeError):
            continue

        tool_calls = meta.get("tool_calls", [])
        if not tool_calls:
            continue

        if "llm_tool_calls" in meta:
            continue

        llm_tool_calls = []
        for tc in tool_calls:
            llm_tool_calls.append({
                "id": tc.get("id", str(uuid.uuid4())[:8]),
                "type": "function",
                "function": {
                    "name": tc.get("name", ""),
                    "arguments": tc.get("args", "{}"),
                },
            })

        meta["llm_tool_calls"] = llm_tool_calls
        meta["display_tool_calls"] = tool_calls  # safety net

        conn.execute(
            "UPDATE messages SET metadata = ? WHERE id = ?",
            (json.dumps(meta), msg_id)
        )
        migrated += 1

        for tc in tool_calls:
            tc_id = tc.get("id", "")
            tc_name = tc.get("name", "")
            tc_result = tc.get("result")
            if tc_result is None:
                tc_result = ""

            tool_msg_id = str(uuid.uuid4())[:8]
            tool_meta = json.dumps({
                "tool_call_id": tc_id,
                "name": tc_name,
            })

            conn.execute(
                "INSERT INTO messages (id, session_id, role, content, metadata, created_at) VALUES (?,?,?,?,?,?)",
                (tool_msg_id, session_id, "tool", tc_result, tool_meta, created_at)
            )
            tool_rows_inserted += 1

    conn.commit()
    conn.close()

    print(f"Migration complete:")
    print(f"  {migrated} assistant messages updated with llm_tool_calls")
    print(f"  {tool_rows_inserted} tool rows inserted")


if __name__ == "__main__":
    migrate()
