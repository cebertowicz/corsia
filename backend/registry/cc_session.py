"""Atomic ownership of one Claude Code provider identity per CORSIA thread.

Each launch stores its description (``claude_invocation``) on the session
record. The thread row owns confinement for subsequent launches.
"""

from __future__ import annotations

from loguru import logger

from infra.database import write_transaction
from registry import models
from registry.notifications import notify_changed


class CCRegistrationChanged(RuntimeError):
    """The CC identity changed after launch preparation."""


def persist_cc_session(
    thread_id: str | None,
    cc_session_id: str | None,
    *,
    title: str | None = None,
    cwd: str | None = None,
    invocation: object | None = None,
    checkpoint_generation_id: str | None = None,
    expected_previous_id: str | None = None,
    verify_previous: bool = False,
    db=None,
) -> tuple[str | None, dict | None]:
    """Atomically replace a thread's current CC continuity artifact."""
    if not thread_id:
        return None, None
    row = None
    with write_transaction(db, scope="replace_cc_session") as connection:
        current = models.find_for_thread(
            thread_id, "claude-code", "session", db=connection
        )
        previous_id = current["external_id"] if current else None
        if verify_previous and previous_id != expected_previous_id:
            raise CCRegistrationChanged("Claude Code continuity registration changed")
        if current is not None and previous_id != cc_session_id:
            models.retire(current["id"], db=connection)
        if cc_session_id is not None:
            metadata = {"cwd": cwd} if cwd else {}
            if invocation is not None:
                from subagents.cc_invocation import claude_invocation_metadata

                # Describes this process; the thread row owns future confinement.
                metadata["claude_invocation"] = claude_invocation_metadata(invocation)
            if checkpoint_generation_id is not None:
                metadata["checkpoint_generation_id"] = checkpoint_generation_id
            row = models.register_artifact(
                integration="claude-code",
                type="session",
                external_id=cc_session_id,
                parent_thread_id=thread_id,
                title=title,
                metadata=metadata or None,
                db=connection,
            )
            if invocation is not None or checkpoint_generation_id is not None:
                row = models.update_metadata(
                    "claude-code", "session", cc_session_id, metadata,
                    db=connection,
                )
    return previous_id, row


def notify_cc_session(
    thread_id: str | None, cc_session_id: str | None, row: dict | None,
) -> None:
    """Best-effort notify the UI of a persisted Claude Code session."""
    if not thread_id or cc_session_id is None:
        return
    try:
        notify_changed(row or {
            "integration": "claude-code", "type": "session",
            "external_id": cc_session_id})
    except Exception as error:
        logger.warning(
            f"registry cc session notify failed for {thread_id}: {error}")
