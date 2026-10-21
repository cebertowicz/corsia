"""The reads rule evaluation makes once a rule matched: artifact, threads, locality.

Each is one indexed read, made only after ``events.rules`` found a matching
rule, so an event no rule matches never pays them. Names no connector.
"""

from __future__ import annotations

import sqlite3
from collections.abc import Iterable, Mapping
from typing import Any

from infra.queries import thread_available_sql

# The registration event the registry appends when an artifact ends.
_ARTIFACT_ENDED = "artifact_ended"
_OWNER_STATES = ("owner-pinned", "owner-active")


def artifact_context(db: sqlite3.Connection, ref: Mapping[str, str] | None) -> tuple[str | None, bool]:
    """The artifact's registered owner and whether it has ended, in one read."""
    if not ref:
        return None, False
    row = db.execute(
        """SELECT
              (SELECT r.parent_thread_id FROM artifact_registrations r
                WHERE r.artifact_id=i.id AND r.relationship_state IN (?, ?)
                ORDER BY CASE r.relationship_state WHEN 'owner-pinned' THEN 0 ELSE 1 END,
                         r.updated_at DESC
                LIMIT 1) AS owner,
              EXISTS (SELECT 1 FROM artifact_registration_events e
                       WHERE e.artifact_id=i.id AND e.event_type=?) AS ended
            FROM artifact_identities i
           WHERE i.integration=? AND i.type=? AND i.external_id=? AND i.state='active'""",
        (*_OWNER_STATES, _ARTIFACT_ENDED, ref["integration"], ref["type"], ref["external_id"]),
    ).fetchone()
    if row is None:
        return None, False
    return row["owner"], bool(row["ended"])


def threads(db: sqlite3.Connection, ids: Iterable[str | None]) -> dict[str, Mapping[str, Any]]:
    """Each named thread's project, locality and availability, in one read."""
    wanted = sorted({thread for thread in ids if thread})
    if not wanted:
        return {}
    rows = db.execute(
        f"SELECT id, project_id, local_only, {thread_available_sql()} AS available "
        f"FROM threads WHERE id IN ({','.join('?' * len(wanted))})",
        wanted,
    ).fetchall()
    return {row["id"]: row for row in rows}


def cloud_off(db: sqlite3.Connection, projects: Iterable[str | None]) -> set[str]:
    """The projects among ``projects`` whose cloud models are off (R29)."""
    wanted = sorted({project for project in projects if project})
    if not wanted:
        return set()
    rows = db.execute(
        "SELECT project_id FROM project_inference_policies WHERE cloud_models_off=1 "
        f"AND project_id IN ({','.join('?' * len(wanted))})",
        wanted,
    ).fetchall()
    return {row["project_id"] for row in rows}
