"""Atomic ownership of one Claude Code provider identity per CORSIA thread."""

from __future__ import annotations

from infra.database import write_transaction
from registry import models


class CCRegistrationChanged(RuntimeError):
    """The CC identity changed after launch preparation."""


def replace_cc_session(
    thread_id: str,
    cc_session_id: str | None,
    *,
    title: str | None,
    cwd: str | None,
    invocation: object | None,
    checkpoint_generation_id: str | None,
    expected_previous_id: str | None,
    verify_previous: bool,
    db=None,
) -> tuple[str | None, dict | None]:
    """Replace the active identity transactionally and return prior/current rows."""
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
