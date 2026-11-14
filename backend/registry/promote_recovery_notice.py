"""Tell the promoting agent when startup recovery undid its promote or moved its file.

A promote that crashes mid-way is finished or undone at the next startup, with the agent that
asked for it long gone. Without a notice the file simply is not where that agent left it. The
agent hears through two routes every thread already has: a note in its own context journal
(`append_thread_context_note`, offered again if the digest carrying it never reached the model)
and a change on the promoted document's identity (`record_artifact_change`), which is the
document's own history. A person who owns the document hears only through those: the agent can
tell them, and the document's history shows it. There is no person-addressed channel here.
"""

from __future__ import annotations

import logging
from pathlib import Path

from infra.database import write_transaction
from infra.passive_changes import REOFFER_UNWRITTEN, append_thread_context_note
from registry import models, service

logger = logging.getLogger(__name__)

INTERRUPTED = "It was interrupted by a restart before it finished."
PARKED = "A newer file was already at the source path, so your file was kept beside it."
PARKED_IN_HISTORY = "A newer file was already at the source path, so the file was kept beside it."


def recovery_path(source: Path, intent_id: str) -> Path:
    """Where recovery parks a claimed file that cannot go back over a newer one."""
    return source.with_name(f"{source.name}.promotion-recovery-{intent_id}")


def file_now_at(source: Path, intent_id: str) -> Path:
    """Where the promoted file is after recovery: its source path, or the parked copy beside a newer file."""
    parked = recovery_path(source, intent_id)
    return parked if parked.exists() else source


def announce_undone_promote(db, row: dict, *, why: str, source: Path, target: Path) -> None:
    """Tell the promoting agent what recovery undid, why, and where its file is now.

    Never raises: a notice that cannot be written must not stop startup on every boot, so the
    failure is logged and recovery goes on.
    """
    intent_id = str(row["id"])
    where = file_now_at(source, intent_id)
    thread_id = str(row["actor_thread_id"])
    parked = f"{PARKED} " if where != source else ""
    summary = (
        f"Your promote of `{source}` to `{target}` was undone when CORSIA restarted. {why} "
        f"{parked}Your file is at `{where}`."
    )
    # The document's history is read by every agent that follows the document and by the person,
    # none of whom made the promote, so it says whose promote it was instead of "your".
    history = (
        f"The promote of `{source}` to `{target}` by thread {thread_id} was undone when CORSIA "
        f"restarted. {why} {PARKED_IN_HISTORY + ' ' if where != source else ''}The file is at `{where}`."
    )
    details = {
        "intent_id": intent_id,
        "source_path": str(source),
        "target_path": str(target),
        "file_now_at": str(where),
        REOFFER_UNWRITTEN: True,
    }
    identity = None
    try:
        identity = service.find_file_identity(str(source), db=db)
        with write_transaction(db, scope="promote_recovery_notice") as connection:
            append_thread_context_note(
                connection, thread_id=thread_id, summary=summary, details=details,
            )
            if identity is not None:
                models.record_artifact_change(
                    artifact_id=identity["id"],
                    internal_event="promote_undone",
                    external_id=identity.get("external_id"),
                    summary=history,
                    provenance=details,
                    # The promoting agent already has the note above; naming it as the actor keeps
                    # its digest from carrying the same event a second time.
                    actor_thread_id=thread_id,
                    db=connection,
                )
    except Exception:
        logger.warning(
            "Could not tell the agent that promotion %s was undone (artifact %s, file at %s).",
            intent_id, identity["id"] if identity else "unknown", where, exc_info=True,
        )
