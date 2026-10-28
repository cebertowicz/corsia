"""Schema 158: stamp a ``cause_kind`` on provider-failure rows written before rows carried one.

Every ``llm_error`` and ``cc_error`` writer, and the Claude Code ``result``
writer for a failure the person did not stop, now stores ``cause_kind`` (from
``llm.error_cause``). Older rows have only their ``cause`` and the error text
they showed, so each is classified once, through the same mapping the writers
use, and a reader keys on the stored field alone. A row that already has a
``cause_kind``, or whose metadata is not a JSON object, is left as it is.
"""
from __future__ import annotations

import json
import sqlite3

from llm.error_cause import cause_kind, harness_cause_kind

_UNSTAMPED_SQL = (
    "SELECT id, content, metadata FROM messages WHERE role='system' "
    "AND json_valid(metadata) AND json_type(metadata)='object' "
    "AND (json_extract(metadata, '$.type') IN ('llm_error', 'cc_error') "
    "OR (json_extract(metadata, '$.type')='result' AND json_type(metadata, '$.cc')='true' "
    "AND json_type(metadata, '$.is_error')='true' "
    "AND coalesce(json_extract(metadata, '$.stop_reason'), '') != 'interrupted')) "
    "AND json_type(metadata, '$.cause_kind') IS NULL"
)


def _harness(metadata: dict) -> str | None:
    if metadata.get("harness") in ("claude-code", "codex"):
        return str(metadata["harness"])
    if metadata.get("codex") is True:
        return "codex"
    return "claude-code" if metadata.get("cc") is True else None


def stored_cause_kind(content: object, metadata: dict) -> str:
    """The kind a writer would have stored: harness rows also read their error text."""
    cause = metadata.get("cause")
    harness = _harness(metadata) if metadata.get("type") in ("cc_error", "result") else None
    if harness is None:
        return cause_kind(cause)
    return harness_cause_kind(harness, cause if isinstance(cause, str) else None, content)


def migrate_157_to_158(conn: sqlite3.Connection) -> None:
    """Stamp each unstamped row inside the caller's migration transaction.

    A database without ``messages`` has no rows to stamp.
    """
    if conn.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='messages'").fetchone() is None:
        return
    rows = conn.execute(_UNSTAMPED_SQL).fetchall()
    conn.executemany(
        "UPDATE messages SET metadata=json_set(metadata, '$.cause_kind', ?) WHERE id=?",
        [(stored_cause_kind(content, json.loads(metadata)), row_id) for row_id, content, metadata in rows],
    )
