"""Shared scaffolding for the registry test files (pane / observer / api).

One place owns the raw session INSERT and the production-path CC child
harness so a sessions-schema change or a cc_runtime seam change is a
one-file fix.
"""

from __future__ import annotations

import asyncio
import uuid
from datetime import datetime, timezone

from core.turn_core import persist_turn_start
from subagents.cc_runtime_events import _make_on_event
from subagents.cc_runtime_state import _Live
from subagents.child_thread import create_child_thread


ARTIFACT_REGISTRY_DDL = """
CREATE TABLE artifact_identities (
    id TEXT PRIMARY KEY, integration TEXT NOT NULL, type TEXT NOT NULL,
    external_id TEXT NOT NULL, title TEXT, description TEXT, path TEXT,
    state TEXT NOT NULL DEFAULT 'active',
    retention TEXT NOT NULL DEFAULT 'durable'
        CHECK (retention IN ('ephemeral', 'durable')),
    metadata TEXT,
    created_at TEXT NOT NULL, updated_at TEXT NOT NULL,
    UNIQUE(integration, type, external_id)
);
CREATE TABLE artifact_registrations (
    id TEXT PRIMARY KEY,
    artifact_id TEXT NOT NULL REFERENCES artifact_identities(id),
    parent_thread_id TEXT NOT NULL,
    relationship_state TEXT NOT NULL DEFAULT 'owner-active',
    tmp INTEGER NOT NULL DEFAULT 0,
    surfacing TEXT NOT NULL DEFAULT 'visible'
        CHECK (surfacing IN ('visible', 'quiet', 'settled')),
    origin_plan_node_id TEXT,
    retired_at TEXT,
    metadata TEXT,
    created_at TEXT NOT NULL, updated_at TEXT NOT NULL,
    UNIQUE(artifact_id, parent_thread_id)
);
CREATE INDEX idx_artifact_registrations_thread
    ON artifact_registrations(parent_thread_id, updated_at DESC);
CREATE INDEX idx_artifact_registrations_artifact
    ON artifact_registrations(artifact_id, relationship_state);
CREATE TABLE artifact_registration_events (
    id TEXT PRIMARY KEY,
    artifact_id TEXT NOT NULL REFERENCES artifact_identities(id),
    event_type TEXT NOT NULL, actor_thread_id TEXT, authority TEXT,
    reason TEXT, source_path TEXT, causal_event_id TEXT,
    affected_thread_id TEXT, old_owner_thread_id TEXT,
    new_owner_thread_id TEXT, old_relationship_state TEXT,
    new_relationship_state TEXT, metadata TEXT, created_at TEXT NOT NULL
);
CREATE INDEX idx_artifact_registration_events_artifact
    ON artifact_registration_events(artifact_id, created_at DESC);
CREATE INDEX idx_artifact_registration_events_thread
    ON artifact_registration_events(affected_thread_id, created_at DESC);
"""


def seed_artifact(
    db,
    *,
    artifact_id: str,
    integration: str,
    type: str,
    external_id: str,
    parent_thread_id: str,
    title: str | None = None,
    description: str | None = None,
    path: str | None = None,
    state: str = "active",
    retention: str = "durable",
    metadata: str | None = None,
    relationship_state: str = "owner-active",
    now: str = "t",
) -> None:
    """Seed one normalized artifact without exercising registry behavior."""
    db.execute(
        """INSERT INTO artifact_identities
           (id,integration,type,external_id,title,description,path,state,retention,
            metadata,created_at,updated_at) VALUES (?,?,?,?,?,?,?,?,?,?,?,?)""",
        (
            artifact_id, integration, type, external_id, title, description,
            path, state, retention, metadata, now, now,
        ),
    )
    db.execute(
        """INSERT INTO artifact_registrations
           (id,artifact_id,parent_thread_id,relationship_state,metadata,
            created_at,updated_at) VALUES (?,?,?,?,NULL,?,?)""",
        (
            f"reg:{artifact_id}", artifact_id, parent_thread_id,
            relationship_state, now, now,
        ),
    )


def artifact_rows_for_thread(db, thread_id: str) -> list[dict]:
    return [
        dict(row)
        for row in db.execute(
            """SELECT i.*, r.parent_thread_id, r.surfacing, r.relationship_state,
                      r.metadata AS registration_metadata
               FROM artifact_identities i
               JOIN artifact_registrations r ON r.artifact_id=i.id
               WHERE r.parent_thread_id=? ORDER BY i.updated_at DESC, i.id ASC""",
            (thread_id,),
        ).fetchall()
    ]


def make_session(db, *, project_id: str | None = None,
                 title: str = "owning thread", prefix: str = "reg",
                 user_id: str = "steve") -> str:
    """Insert a bare owning thread (scaffolding, not the subject under test).

    ``user_id`` defaults to the single operator; pass another id (e.g.
    ``"leksi"``) to build a second user's thread for cross-user authz tests.
    """
    sid = f"{prefix}-{uuid.uuid4().hex[:8]}"
    now = datetime.now(timezone.utc).isoformat()
    db.execute(
        "INSERT INTO threads "
        "(id,user_id,title,project_id,model,created_at,updated_at) "
        "VALUES (?,?,?,?,?,?,?)",
        (sid, user_id, title, project_id, "native/test", now, now),
    )
    db.commit()
    return sid


def noop_bcast(thread_id: str, message: dict) -> None:
    pass


def cc_child(db, parent: str, *, title: str = "subagent"):
    """Create a CC child the way production does — create_child_thread plus
    the REAL live event handler. Returns ``(child_id, on_event)``.

    The returned ``on_event`` mirrors the production sequence for tool
    results: the backend bridge persists the canonical result bytes at
    execution time, before Claude Code can echo the tool_result event, so the
    echo binds to an existing row instead of arriving as an orphan.
    """
    from cc_runtime_fixtures import persist_bridge_tool_result
    from infra.input_writes import begin_batch_write, settle_batch_write
    from infra.thread_driver import admit_input

    child = create_child_thread(
        user_id="steve",
        parent_thread_id=parent,
        title=title,
        model="claude-code/sonnet",
        db=db,
    )
    live = _Live(session=None, seed_file="", parent_thread_id=parent, name=title)
    turn_id = str(uuid.uuid4())
    admission = admit_input(
        child,
        role="user",
        content=title,
        intent="apply_now",
        message_id=f"{turn_id}-input",
        turn_id=turn_id,
        boot_epoch="registry-test",
        admitted_at="t",
        db=db,
    )
    assert admission.batch is not None
    identity = persist_turn_start(
        turn_id, child, "claude-code/sonnet", title, db=db,
    )
    attempt_id = f"{turn_id}-write"
    begin_batch_write(admission.batch, attempt_id=attempt_id, started_at="t", db=db)
    settle_batch_write(attempt_id, outcome="accepted", db=db)
    live.corsia_turn = identity
    live.turn_terminal = asyncio.Event()
    live.turn_active = True
    on_event = _make_on_event(live, child, noop_bcast, db)

    async def on_event_with_bridge_execution(ev):
        if (
            ev.get("kind") == "tool_result"
            and ev.get("tool_use_id", "") in live.tool_step_ids
        ):
            persist_bridge_tool_result(
                db, live,
                thread_id=child,
                provider_tool_call_id=ev["tool_use_id"],
                content=ev.get("content", ""),
                is_error=bool(ev.get("is_error")),
            )
        await on_event(ev)

    return child, on_event_with_bridge_execution
