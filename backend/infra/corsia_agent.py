"""The CORSIA agent's designation: the one workspace-scope agent of an install.

One thread per install is designated in the ``corsia_agent_thread_id`` settings
row. A project's conductor is a different role, designated on the project row
(``infra.conductor``). Corrupt designation state must fail loudly so the agent
cannot silently lose its operating instructions.
"""

from __future__ import annotations

import json
from datetime import datetime, timezone

from infra.database import get_db, write_transaction
from infra.fresh_threads import create_fresh_thread
from infra.queries import thread_is_openable

SETTINGS_KEY = "corsia_agent_thread_id"
THREAD_TITLE = "CORSIA"


class CorsiaAgentSettingError(ValueError):
    """The persisted CORSIA agent designation is corrupt."""


def corsia_agent_thread_id(row) -> str | None:
    """Decode one persisted designation without hiding corruption."""
    try:
        value = json.loads(row["value"])
    except (json.JSONDecodeError, TypeError, UnicodeDecodeError) as error:
        raise CorsiaAgentSettingError("CORSIA agent setting is corrupt") from error
    if not isinstance(value, dict):
        raise CorsiaAgentSettingError("CORSIA agent setting is corrupt")
    thread_id = value.get("session_id")
    if thread_id is not None and (
        not isinstance(thread_id, str) or not thread_id.strip()
    ):
        raise CorsiaAgentSettingError("CORSIA agent setting is corrupt")
    return thread_id


def get_corsia_agent(db=None) -> str | None:
    """Return the designated CORSIA agent's thread id, or None."""
    _db = db or get_db()
    try:
        row = _db.execute(
            "SELECT value FROM settings WHERE key = ?", (SETTINGS_KEY,)
        ).fetchone()
        return corsia_agent_thread_id(row) if row else None
    finally:
        if not db:
            _db.close()


def is_corsia_agent(thread_id: str, db=None) -> bool:
    """Whether ``thread_id`` is the designated CORSIA agent."""
    return thread_id is not None and get_corsia_agent(db=db) == thread_id


def set_corsia_agent(thread_id: str | None, db=None) -> bool:
    """Designate (or clear, with None) the CORSIA agent thread.

    Always True: the workspace scope always exists. The prior and the new
    designation each get a fresh runtime context epoch, and every project agent
    hears that the contact changed.
    """
    from infra.conductor import bump_context_epochs
    from infra.passive_changes import append_corsia_agent_contact_change

    if db is not None and db.in_transaction:
        # The prior designation and both epoch invalidations are one serialized
        # state transition. A caller-owned SQLite transaction may still be
        # DEFERRED/read-only, so acquire (or verify) its write ownership before
        # the read without mutating canonical state or firing row triggers.
        db.execute("UPDATE settings SET value=value WHERE 0")
    with write_transaction(db, scope="corsia_agent_designation") as _db:
        now = datetime.now(timezone.utc).isoformat()
        previous = get_corsia_agent(db=_db)
        if previous == thread_id:
            return True
        _db.execute(
            "INSERT INTO settings (key, value, updated_at) VALUES (?, ?, ?) "
            "ON CONFLICT(key) DO UPDATE SET "
            "value=excluded.value, updated_at=excluded.updated_at",
            (SETTINGS_KEY, json.dumps({"session_id": thread_id}), now),
        )
        invalidated = bump_context_epochs(_db, (previous, thread_id), now)
        if invalidated:
            placeholders = ",".join("?" for _ in invalidated)
            contact_users = _db.execute(
                f"SELECT DISTINCT user_id FROM threads WHERE id IN ({placeholders})",
                invalidated,
            ).fetchall()
            for row in contact_users:
                append_corsia_agent_contact_change(
                    _db,
                    user_id=str(row["user_id"]),
                    summary="Designated CORSIA agent changed",
                    thread_id=thread_id,
                )
        return True


def ensure_corsia_agent(user_id: str, *, model: str | None, db=None) -> str | None:
    """Create and designate the CORSIA agent thread when the install has none.

    An existing openable designation is preserved. ``user_id`` owns the created
    thread. ``model`` is its selector: a thread created with ``None`` holds its
    first input until the person chooses a model, so only a caller with no one
    waiting on the agent passes it.
    """
    _db = db or get_db()
    try:
        with write_transaction(_db, scope="ensure_corsia_agent") as conn:
            existing = get_corsia_agent(db=conn)
            if existing and thread_is_openable(existing, db=conn):
                return existing
            session = create_fresh_thread(
                user_id=user_id,
                title=THREAD_TITLE,
                model=model,
                project_id=None,
                db=conn,
            )
            set_corsia_agent(session["id"], db=conn)
            return session["id"]
    finally:
        if not db:
            _db.close()
