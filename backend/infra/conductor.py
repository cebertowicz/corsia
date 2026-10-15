"""Conductor designation storage and scope projection.

ONE designated conductor thread per scope:

- ``workspace``      → the ``workspace_conductor_session_id`` settings row
- ``<project id>``   → ``projects.conductor_session_id``

Consumers include attach/routing, conductor status, and source-managed context
template selection. Corrupt designation state must fail loudly so a conductor
cannot silently lose its operating instructions.
"""

from __future__ import annotations

import json
from datetime import datetime, timezone
from typing import NotRequired, TypedDict

from infra.database import get_db, write_transaction
from infra.fresh_threads import create_fresh_thread
from infra.queries import thread_is_openable

WORKSPACE_SCOPE = "workspace"
_WORKSPACE_KEY = "workspace_conductor_session_id"


class ConductorSettingError(ValueError):
    """The persisted workspace conductor designation is corrupt."""


class ConductorScope(TypedDict):
    """One scope that designates a thread as its conductor."""

    scope: str
    scope_type: str
    project_name: NotRequired[str]


def workspace_conductor_id(row) -> str | None:
    """Decode one persisted workspace designation without hiding corruption."""
    try:
        value = json.loads(row["value"])
    except (json.JSONDecodeError, TypeError, UnicodeDecodeError) as error:
        raise ConductorSettingError("workspace conductor setting is corrupt") from error
    if not isinstance(value, dict):
        raise ConductorSettingError("workspace conductor setting is corrupt")
    thread_id = value.get("session_id")
    if thread_id is not None and (
        not isinstance(thread_id, str) or not thread_id.strip()
    ):
        raise ConductorSettingError("workspace conductor setting is corrupt")
    return thread_id


def get_conductor(scope: str = WORKSPACE_SCOPE, db=None) -> str | None:
    """Return the designated conductor session id for a scope, or None.

    ``scope`` is ``'workspace'`` or a project id. An unknown project id
    reads as None (no designation) — GET is not a validation surface.
    """
    _db = db or get_db()
    try:
        if scope == WORKSPACE_SCOPE:
            row = _db.execute(
                "SELECT value FROM settings WHERE key = ?", (_WORKSPACE_KEY,)
            ).fetchone()
            if not row:
                return None
            return workspace_conductor_id(row)
        row = _db.execute(
            "SELECT conductor_thread_id FROM projects WHERE id = ?", (scope,)
        ).fetchone()
        return row["conductor_thread_id"] if row else None
    finally:
        if not db:
            _db.close()


def get_all_conductor_ids(db=None) -> list[str]:
    """Return all designated conductor session IDs across all scopes.

    Reads the workspace conductor from settings and per-project conductors
    from the projects table. Used by the session list endpoint to exclude
    conductors from the chat sidebar (they're surfaced separately).
    Returns a deduplicated list; empty when nothing is designated.
    """
    _db = db or get_db()
    try:
        ids: set[str] = set()
        workspace_id = get_conductor(WORKSPACE_SCOPE, db=_db)
        if workspace_id:
            ids.add(workspace_id)
        rows = _db.execute(
            "SELECT conductor_thread_id FROM projects WHERE conductor_thread_id IS NOT NULL"
        ).fetchall()
        for row in rows:
            ids.add(row["conductor_thread_id"])
        return list(ids)
    finally:
        if not db:
            _db.close()


def get_conductor_scopes(thread_id: str, db=None) -> list[ConductorScope]:
    """Return scopes that designate ``session_id`` as their conductor.

    This is runtime/debug metadata for status surfaces. It does not decide
    dispatch; it tells callers whether the addressed thread is the workspace
    conductor, one or more project conductors, or an ordinary thread.
    """
    _db = db or get_db()
    try:
        scopes: list[ConductorScope] = []
        row = _db.execute(
            "SELECT value FROM settings WHERE key = ?", (_WORKSPACE_KEY,)
        ).fetchone()
        if row and workspace_conductor_id(row) == thread_id:
            scopes.append({"scope": WORKSPACE_SCOPE, "scope_type": WORKSPACE_SCOPE})

        rows = _db.execute(
            "SELECT id, name FROM projects WHERE conductor_thread_id = ? "
            "ORDER BY name, id",
            (thread_id,),
        ).fetchall()
        for row in rows:
            scopes.append(
                {
                    "scope": row["id"],
                    "scope_type": "project",
                    "project_name": row["name"],
                }
            )
        return scopes
    finally:
        if not db:
            _db.close()


def human_conductor_model(db=None) -> str | None:
    """Return the workspace conductor's eligible selected model, if any.

    A human-created project inherits only the workspace conductor's explicit
    ready selection. Without one, its conductor stays model-less and holds
    input until the user chooses a model.
    """
    _db = db or get_db()
    try:
        thread_id = get_conductor(WORKSPACE_SCOPE, db=_db)
        row = (
            _db.execute("SELECT model FROM threads WHERE id = ?", (thread_id,)).fetchone()
            if thread_id else None
        )
        selected_model = row["model"] if row is not None else None
    finally:
        if not db:
            _db.close()
    if not selected_model:
        return None
    from llm.delegation_model import eligible_delegation_model

    canonical, _refusal = eligible_delegation_model(selected_model)
    return canonical


def ensure_scope_conductor(
    scope: str,
    user_id: str,
    *,
    model: str | None,
    db=None,
) -> str | None:
    """Create and designate a conductor thread when a scope lacks one.

    Existing openable designations are preserved. Unknown project scopes return
    ``None`` so callers can keep using ``set_conductor`` as the validation
    surface for explicit re-designation. ``user_id`` names the account that
    owns the created thread; every caller must resolve one, because a scope
    without an owner to create for is a caller error, not a state to skip.
    ``model`` is the created thread's selector. Every caller states it: a
    thread created with ``None`` holds its first input until the user chooses
    a model, so only a caller with no one waiting on the conductor passes it.
    """
    _db = db or get_db()
    try:
        with write_transaction(_db, scope="ensure_scope_conductor") as conn:
            existing = get_conductor(scope, db=conn)
            if existing and thread_is_openable(existing, db=conn):
                return existing

            project_id = None
            if scope != WORKSPACE_SCOPE:
                row = conn.execute(
                    "SELECT id FROM projects WHERE id = ?",
                    (scope,),
                ).fetchone()
                if row is None:
                    return None
                project_id = row["id"]

            session = create_fresh_thread(
                user_id=user_id,
                title="Conductor",
                model=model,
                project_id=project_id,
                db=conn,
            )
            if not set_conductor(scope, session["id"], db=conn):
                raise RuntimeError(f"unknown conductor scope: {scope!r}")
            return session["id"]
    finally:
        if not db:
            _db.close()


def backfill_scope_conductors(db=None) -> list[tuple[str, str]]:
    """Heal scopes that predate creation-time conductors, once per install.

    The workspace conductor is owned by the install's first account. Each
    healed project's conductor thread is owned by that project's earliest
    account-backed thread owner — an existing member, so healing grants no
    new project membership through ``user_in_project``. A project with no
    account-backed member has no owner to prefer and falls back to the first
    account. Installs with no accounts are left untouched: projects cannot
    predate their creator's account.

    Called only from the schema v37→v38 data migration. A deliberate
    ``PUT /api/conductor`` clear (``thread_id: null``) must survive every
    later restart, so no recurring startup pass may treat a missing
    designation as one to heal.
    """
    _db = db or get_db()
    try:
        healed: list[tuple[str, str]] = []
        with write_transaction(_db, scope="conductor_backfill") as conn:
            first_user = conn.execute(
                "SELECT id FROM users ORDER BY created_at, id LIMIT 1"
            ).fetchone()
            if first_user is None:
                return healed

            if get_conductor(WORKSPACE_SCOPE, db=conn) is None:
                # Healing is a migration with no turn waiting on the thread;
                # the user chooses its model when they first open it.
                thread_id = ensure_scope_conductor(
                    WORKSPACE_SCOPE, first_user["id"], model=None, db=conn
                )
                if thread_id is not None:
                    healed.append((WORKSPACE_SCOPE, thread_id))

            rows = conn.execute(
                "SELECT id FROM projects WHERE conductor_thread_id IS NULL "
                "ORDER BY name, id"
            ).fetchall()
            for row in rows:
                owner = conn.execute(
                    "SELECT t.user_id FROM threads t "
                    "JOIN users u ON u.id = t.user_id "
                    "WHERE t.project_id = ? ORDER BY t.created_at, t.id LIMIT 1",
                    (row["id"],),
                ).fetchone()
                thread_id = ensure_scope_conductor(
                    row["id"],
                    owner["user_id"] if owner else first_user["id"],
                    model=None,
                    db=conn,
                )
                if thread_id is not None:
                    healed.append((row["id"], thread_id))
        return healed
    finally:
        if not db:
            _db.close()


def set_conductor(scope: str, thread_id: str | None, db=None) -> bool:
    """Designate (or clear, with None) a scope's conductor thread.

    Returns False when ``scope`` names a project that doesn't exist —
    callers decide whether that's a 400 or an exception. The workspace
    scope always exists.
    """
    if db is not None and db.in_transaction:
        # The prior designation and both epoch invalidations are one serialized
        # state transition. A caller-owned SQLite transaction may still be
        # DEFERRED/read-only, so acquire (or verify) its write ownership before
        # the read without mutating canonical state or firing row triggers.
        db.execute("UPDATE settings SET value=value WHERE 0")
    with write_transaction(db, scope="conductor_designation") as _db:
        now = datetime.now(timezone.utc).isoformat()
        if scope == WORKSPACE_SCOPE:
            previous = get_conductor(WORKSPACE_SCOPE, db=_db)
            if previous == thread_id:
                return True
            _db.execute(
                "INSERT INTO settings (key, value, updated_at) VALUES (?, ?, ?) "
                "ON CONFLICT(key) DO UPDATE SET "
                "value=excluded.value, updated_at=excluded.updated_at",
                (_WORKSPACE_KEY, json.dumps({"session_id": thread_id}), now),
            )
        else:
            row = _db.execute(
                "SELECT conductor_thread_id FROM projects WHERE id = ?",
                (scope,),
            ).fetchone()
            if row is None:
                return False
            previous = row["conductor_thread_id"]
            if previous == thread_id:
                return True
            _db.execute(
                "UPDATE projects SET conductor_thread_id = ?, updated_at = ? "
                "WHERE id = ?",
                (thread_id, now, scope),
            )

        invalidated = tuple(
            designated
            for designated in dict.fromkeys((previous, thread_id))
            if designated is not None
        )
        if invalidated:
            placeholders = ",".join("?" for _ in invalidated)
            _db.execute(
                "UPDATE threads SET runtime_context_epoch="
                "runtime_context_epoch+1, updated_at=? "
                f"WHERE id IN ({placeholders})",
                (now, *invalidated),
            )
        if scope == WORKSPACE_SCOPE and invalidated:
            from infra.passive_changes import append_workspace_conductor_contact_change

            placeholders = ",".join("?" for _ in invalidated)
            contact_users = _db.execute(
                f"SELECT DISTINCT user_id FROM threads WHERE id IN ({placeholders})",
                invalidated,
            ).fetchall()
            for row in contact_users:
                append_workspace_conductor_contact_change(
                    _db,
                    user_id=str(row["user_id"]),
                    summary="Designated workspace conductor changed",
                    thread_id=thread_id,
                )
        return True
