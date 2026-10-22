"""Atomic ownership of one Claude Code provider identity per CORSIA thread.

Each launch stores its description (``claude_invocation``) on the session
record it registers. ``registered_confinement`` is the one reader of a
thread's confinement from those records, and the rule is sticky: a thread is
``review`` if ANY session record it ever registered says so, in any identity
or relationship state, with the add-dirs and working directory of the newest
such record. Every launch reads it once, at its start.

| Records the thread registered | Reads as |
|---|---|
| none | none |
| records without a ``claude_invocation`` | none |
| newest without an invocation, an older review record | review |
| the review record's session retired | review |
| the review record's registration left the owner states | review |
| a damaged review record (unknown mode, bad add-dirs, no working dir) | refused |
| any record whose metadata is not JSON | refused |

Refused fails the launch: a record that cannot be read could be the one that
said review, so it never reads as none.
"""

from __future__ import annotations

import json

from loguru import logger

from infra.database import write_transaction
from registry import models
from registry.listing import metadata_ever_for_thread
from registry.notifications import notify_changed

#: Where a session record keeps its launch's confinement.
CONFINEMENT_PATH = "$.claude_invocation.confinement"


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


def registered_confinement(thread_id: str, db=None):
    """The thread's launch confinement by the sticky rule above, or refused.

    Raises ``ConfinementRefused`` for a damaged or unreadable record.
    """
    from subagents.cc_confinement import ConfinementRefused
    from subagents.cc_invocation import (
        CONFINEMENT_REVIEW, UNCONFINED, LaunchConfinement, stored_confinement,
    )

    newest = None
    for raw in metadata_ever_for_thread(thread_id, "claude-code", "session", CONFINEMENT_PATH, db=db):
        try:
            invocation = json.loads(raw)["claude_invocation"]
            mode, add_dirs = stored_confinement(invocation)
        except (TypeError, KeyError, AttributeError, ValueError) as error:
            raise ConfinementRefused("Claude Code launch record is damaged or unreadable") from error
        if mode != CONFINEMENT_REVIEW or newest is not None:
            continue
        working_dir = invocation.get("working_dir")
        if not isinstance(working_dir, str) or not working_dir:
            raise ConfinementRefused("Claude Code review launch record is damaged: no working dir")
        newest = LaunchConfinement(mode, add_dirs, working_dir)
    return newest or UNCONFINED
