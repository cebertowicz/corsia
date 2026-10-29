"""Schema 162 -> 163: name unambiguously designated coordination threads."""

import json
import sqlite3
from collections import defaultdict


def migrate_162_to_163(conn: sqlite3.Connection) -> None:
    tables = {row[0] for row in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    if 'threads' not in tables:
        return
    designations: dict[str, list[tuple[str, bool]]] = defaultdict(list)
    setting = conn.execute("SELECT value FROM settings WHERE key='corsia_agent_thread_id'").fetchone() if 'settings' in tables else None
    if setting:
        try:
            value = json.loads(setting[0])
        except (TypeError, ValueError):
            value = None
        thread_id = value.get('session_id') if isinstance(value, dict) else None
        if isinstance(thread_id, str) and thread_id:
            designations[thread_id].append(('CORSIA', True))
    if 'projects' in tables:
        for name, thread_id in conn.execute(
            'SELECT name, conductor_thread_id FROM projects WHERE conductor_thread_id IS NOT NULL'
        ):
            designations[thread_id].append((f'{name} Conductor', False))
    for thread_id, titles in designations.items():
        if len(titles) == 1:
            title, is_workspace = titles[0]
            conn.execute(
                "UPDATE threads SET title=? WHERE id=? AND (lower(trim(title))='conductor' "
                "OR (? AND lower(trim(title))='workspace-conductor'))",
                (title, thread_id, is_workspace),
            )
