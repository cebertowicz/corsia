"""Shared helpers for event-routing test files.

Extracted from test_event_routing.py, test_event_delivery.py, and
test_event_routing_noise_filter.py to prevent schema drift on the
sessions-table column list.
"""

from __future__ import annotations

from contextlib import contextmanager
from datetime import datetime, timezone


def mk_session(thread_id: str, *, spawned_by: str | None = None, model: str = "opus",
               project_id: str | None = None) -> str:
    """Insert a minimal session row and return session_id.

    Subagent-ness derives from ``spawned_by``: pass a parent session id to make
    the row a subagent; leave it ``None`` for a top-level/chat thread.
    """
    from infra.database import get_db
    db = get_db()
    now = datetime.now(timezone.utc).isoformat()
    db.execute(
        """INSERT INTO threads
           (id, user_id, title, spawned_by, model, project_id, created_at, updated_at)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
        (thread_id, "user-1", "T", spawned_by, model, project_id, now, now),
    )
    db.commit()
    db.close()
    return thread_id


def register_pr(repo: str, pr_number: int, owner_thread: str) -> None:
    """Register a GitHub PR artifact to owner_thread."""
    from events.identity import GITHUB_INTEGRATION, PR_TYPE, pr_external_id
    from registry import models
    models.register_artifact(
        integration=GITHUB_INTEGRATION,
        type=PR_TYPE,
        external_id=pr_external_id(repo, pr_number),
        parent_thread_id=owner_thread,
        title=f"PR #{pr_number}",
    )


def pr_payload(repo: str, pr_number: int, *, action: str = "opened",
               author: str = "alice", author_type: str = "User") -> dict:
    """Minimal pr_opened/synchronize payload."""
    return {
        "action": action,
        "repository": {"full_name": repo},
        "pull_request": {
            "number": pr_number, "title": "Test PR",
            "head": {"sha": "abc123", "repo": {"full_name": repo}},
            "base": {"ref": "dev", "repo": {"full_name": repo}}, "labels": [],
        },
        "sender": {"login": author, "type": author_type},
    }


async def route(internal_event: str, payload: dict):
    """Single-call wrapper around route_event."""
    from events.router import route_event
    return await route_event(internal_event, payload)


def check_suite_payload(repo: str, pr_number: int, *,
                        suite_id: int = 9000,
                        conclusion: str = "success",
                        base_ref: str | None = None) -> dict:
    """Build a check_suite.completed payload."""
    pr_entry: dict = {"number": pr_number}
    if base_ref is not None:
        pr_entry["base"] = {"ref": base_ref}
    return {
        "action": "completed",
        "repository": {"full_name": repo},
        "check_suite": {
            "id": suite_id,
            "conclusion": conclusion,
            "status": "completed",
            "pull_requests": [pr_entry],
        },
        "sender": {"login": "alice", "type": "User"},
    }


@contextmanager
def statements():
    """Every statement any pooled connection runs inside the block."""
    import infra.database as database

    seen: list[str] = []
    original = database._make_connection

    def _traced(path):
        conn = original(path)
        conn.set_trace_callback(seen.append)
        return conn

    with database._pool_lock:
        existing = list(database._all_connections)
    for conn in existing:
        conn.set_trace_callback(seen.append)
    database._make_connection = _traced
    try:
        yield seen
    finally:
        database._make_connection = original
        with database._pool_lock:
            current = list(database._all_connections)
        for conn in current:
            conn.set_trace_callback(None)


def read_statements(seen: list[str]) -> list[str]:
    """The reads among the statements ``statements`` captured."""
    return [s for s in seen if s.lstrip().upper().startswith(("SELECT", "WITH"))]


def track_repositories(monkeypatch, *repos: str) -> None:
    """Make ``repos`` the tracked GitHub repositories the PR notice and the quiet
    sweep read (the broker's cached set), without touching a database or a cache."""
    names = frozenset(repos)
    monkeypatch.setattr("connections.github_broker.shared_tracked_repositories", lambda: names)
