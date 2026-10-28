"""Identify a delegated worker's repeated error episode."""
from __future__ import annotations

import hashlib


def error_episode(connection, parent_plan_id: str, worker: str, cause: str) -> str:
    sent = connection.execute(
        """SELECT m.input_order AS last FROM messages AS m JOIN plans AS p ON p.id=?
           WHERE m.thread_id=? AND m.input_order IS NOT NULL AND json_valid(m.metadata)
             AND json_extract(m.metadata,'$.from_thread')=p.owner_id
           ORDER BY m.input_order DESC LIMIT 1""",
        (parent_plan_id, worker),
    ).fetchone()
    key = f"{cause}\n{sent['last'] if sent is not None else None}"
    return f"{worker}:{hashlib.sha256(key.encode()).hexdigest()[:12]}"
