"""Scope designation storage and projection.

ONE designated coordinating thread per scope:

- ``workspace``      → the CORSIA agent (``infra.corsia_agent``)
- ``<project id>``   → the project's conductor, ``projects.conductor_thread_id``

A conductor is a project's coordinator only; the workspace scope's occupant is
the CORSIA agent. The scope-generic functions here let a consumer that does not
care which one it is (attach/routing, status, template selection, ownership)
ask one question per scope. Corrupt designation state must fail loudly so a
designated thread cannot silently lose its operating instructions.
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import NotRequired, TypedDict

from infra.corsia_agent import (
    SETTINGS_KEY as _CORSIA_AGENT_KEY,
    corsia_agent_thread_id,
    ensure_corsia_agent,
    get_corsia_agent,
    set_corsia_agent,
)
from infra.database import get_db, write_transaction
from infra.fresh_threads import create_fresh_thread
from infra.queries import thread_is_openable

WORKSPACE_SCOPE = "workspace"
CONDUCTOR_THREAD_TITLE = "Conductor"


class CoordinationScope(TypedDict):
    """One scope that designates a thread as its coordinating agent."""

    scope: str
    scope_type: str
    project_name: NotRequired[str]


def get_project_conductor(project_id: str, db=None) -> str | None:
    """Return a project's conductor, or None when the project is unknown/unassigned."""
    if project_id == WORKSPACE_SCOPE:
        raise ValueError("workspace scope has a CORSIA agent, not a project conductor")
    _db = db or get_db()
    try:
        row = _db.execute(
            "SELECT conductor_thread_id FROM projects WHERE id = ?", (project_id,)
        ).fetchone()
        return row["conductor_thread_id"] if row else None
    finally:
        if not db:
            _db.close()


def get_all_coordination_ids(db=None) -> list[str]:
    """Return every designated thread id across all scopes.

    Reads the CORSIA agent from settings and per-project conductors from the
    projects table. Used by the session list endpoint to exclude them from the
    chat sidebar (they're surfaced separately). Returns a deduplicated list;
    empty when nothing is designated.
    """
    _db = db or get_db()
    try:
        ids: set[str] = set()
        agent_id = get_corsia_agent(db=_db)
        if agent_id:
            ids.add(agent_id)
        rows = _db.execute(
            "SELECT conductor_thread_id FROM projects WHERE conductor_thread_id IS NOT NULL"
        ).fetchall()
        for row in rows:
            ids.add(row["conductor_thread_id"])
        return list(ids)
    finally:
        if not db:
            _db.close()


def get_coordination_scopes(thread_id: str, db=None) -> list[CoordinationScope]:
    """Return scopes that designate ``thread_id`` as their coordinating agent.

    This is runtime/debug metadata for status surfaces. It does not decide
    dispatch; it tells callers whether the addressed thread is the CORSIA
    agent, one or more project conductors, or an ordinary thread.
    """
    _db = db or get_db()
    try:
        scopes: list[CoordinationScope] = []
        row = _db.execute(
            "SELECT value FROM settings WHERE key = ?", (_CORSIA_AGENT_KEY,)
        ).fetchone()
        if row and corsia_agent_thread_id(row) == thread_id:
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
    """Return the model a human-created project's conductor inherits, if any.

    A human-created project inherits only the CORSIA agent's explicit ready
    selection. Without one, its conductor stays model-less and holds input
    until the user chooses a model.
    """
    _db = db or get_db()
    try:
        thread_id = get_corsia_agent(db=_db)
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


def ensure_project_conductor(
    project_id: str,
    user_id: str,
    *,
    model: str | None,
    db=None,
) -> str | None:
    """Create and designate a project's conductor when it lacks one.

    Existing openable designations are preserved. Unknown project scopes return
    ``None`` so callers can keep using ``set_project_conductor`` as the
    validation surface for explicit re-designation. ``user_id`` names the
    account that owns the created thread; every caller must resolve one,
    because a scope without an owner to create for is a caller error, not a
    state to skip. ``model`` is the created thread's selector. Every caller
    states it: a thread created with ``None`` holds its first input until the
    user chooses a model, so only a caller with no one waiting on it passes
    ``None``.
    """
    if project_id == WORKSPACE_SCOPE:
        raise ValueError("workspace scope has a CORSIA agent, not a project conductor")
    _db = db or get_db()
    try:
        with write_transaction(_db, scope="ensure_project_conductor") as conn:
            existing = get_project_conductor(project_id, db=conn)
            if existing and thread_is_openable(existing, db=conn):
                return existing

            row = conn.execute(
                "SELECT id FROM projects WHERE id = ?",
                (project_id,),
            ).fetchone()
            if row is None:
                return None

            session = create_fresh_thread(
                user_id=user_id,
                title=CONDUCTOR_THREAD_TITLE,
                model=model,
                project_id=row["id"],
                db=conn,
            )
            if not set_project_conductor(project_id, session["id"], db=conn):
                raise RuntimeError(f"unknown project conductor scope: {project_id!r}")
            return session["id"]
    finally:
        if not db:
            _db.close()


def backfill_scope_conductors(db=None) -> list[tuple[str, str]]:
    """Heal scopes that predate creation-time designations, once per install.

    The CORSIA agent is owned by the install's first account. Each healed
    project's conductor thread is owned by that project's earliest
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

            if get_corsia_agent(db=conn) is None:
                # Healing is a migration with no turn waiting on the thread;
                # the user chooses its model when they first open it.
                thread_id = ensure_corsia_agent(
                    first_user["id"], model=None, db=conn
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
                thread_id = ensure_project_conductor(
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


def bump_context_epochs(db, thread_ids, now: str) -> tuple[str, ...]:
    """Give every named thread a fresh runtime context epoch; return them."""
    invalidated = tuple(
        designated
        for designated in dict.fromkeys(thread_ids)
        if designated is not None
    )
    if invalidated:
        placeholders = ",".join("?" for _ in invalidated)
        db.execute(
            "UPDATE threads SET runtime_context_epoch="
            "runtime_context_epoch+1, updated_at=? "
            f"WHERE id IN ({placeholders})",
            (now, *invalidated),
        )
    return invalidated


def set_project_conductor(project_id: str, thread_id: str | None, db=None) -> bool:
    """Designate (or clear) a project's conductor; return False for an unknown project."""
    if project_id == WORKSPACE_SCOPE:
        raise ValueError("workspace scope has a CORSIA agent, not a project conductor")
    if db is not None and db.in_transaction:
        # The prior designation and both epoch invalidations are one serialized
        # state transition. A caller-owned SQLite transaction may still be
        # DEFERRED/read-only, so acquire (or verify) its write ownership before
        # the read without mutating canonical state or firing row triggers.
        db.execute("UPDATE settings SET value=value WHERE 0")
    with write_transaction(db, scope="conductor_designation") as _db:
        now = datetime.now(timezone.utc).isoformat()
        row = _db.execute(
            "SELECT conductor_thread_id FROM projects WHERE id = ?",
            (project_id,),
        ).fetchone()
        if row is None:
            return False
        previous = row["conductor_thread_id"]
        if previous == thread_id:
            return True
        _db.execute(
            "UPDATE projects SET conductor_thread_id = ?, updated_at = ? "
            "WHERE id = ?",
            (thread_id, now, project_id),
        )
        bump_context_epochs(_db, (previous, thread_id), now)
        return True


def get_scope_coordinator(scope: str, db=None) -> str | None:
    """Return the CORSIA agent for workspace or the conductor for a project."""
    if scope == WORKSPACE_SCOPE:
        return get_corsia_agent(db=db)
    return get_project_conductor(scope, db=db)


def set_scope_coordinator(scope: str, thread_id: str | None, db=None) -> bool:
    """Set the CORSIA agent for workspace or a conductor for a project."""
    if scope == WORKSPACE_SCOPE:
        return set_corsia_agent(thread_id, db=db)
    return set_project_conductor(scope, thread_id, db=db)
