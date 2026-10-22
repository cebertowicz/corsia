"""An unfinished first-run setup leaves nothing: the next launch starts again at step 1.

Only an account the app's own setup path made, that nobody has used yet, is reset. Four guards,
each a refusal that leaves everything as it is:

1. **Creation marker.** ``setup_progress`` carries ``created_by_app`` only when the app's setup made
   the account. An adopted, taken-over or older install has none, so it never resets.
2. **No real use yet.** The CORSIA agent's thread is the only thread, and every message in it is a
   system input still held for a model choice (nothing was ever sent or rendered).
3. **Work folder untouched.** Nothing but the files the marker names is removed, and each only while
   its digest still matches what setup wrote. The folder, its other files and its directories stay.
4. **Installed tools stay.** Nothing here touches a CLI, the local model or the classifier.
"""

from __future__ import annotations

import hashlib
import logging
from dataclasses import dataclass
from pathlib import Path

from infra import config, setup_progress
from infra.corsia_agent import SETTINGS_KEY as CORSIA_AGENT_KEY
from infra.corsia_agent import get_corsia_agent
from infra.database import write_transaction
from infra.send_target import NO_MODEL_SELECTED

logger = logging.getLogger(__name__)

#: The default work folder's name under the home folder, as the desktop shell's ``default_workspace`` names it.
DEFAULT_WORK_FOLDER = "CORSIA"


@dataclass(frozen=True)
class ResetResult:
    """Whether the unfinished setup was reset, or the one guard that refused it."""

    reset: bool
    refused: str | None = None
    #: Set when setup's stored work-folder choice is not the default one: the shell restores the default.
    default_work_folder: str | None = None


def _refused(reason: str) -> ResetResult:
    return ResetResult(reset=False, refused=reason)


def _only_held_inputs(db, thread_id: str) -> bool:
    """Whether every message in the thread is a system input no model has read."""
    rows = db.execute(
        "SELECT role, delivery_state, delivery_error FROM messages WHERE thread_id = ?", (thread_id,),
    ).fetchall()
    return all(
        row["role"] == "system" and row["delivery_state"] == "queued" and row["delivery_error"] == NO_MODEL_SELECTED
        for row in rows
    )


def _guard(db, record: dict | None, user_id: str) -> tuple[str | None, str | None]:
    """The refusal reason (or None) and the agent thread the reset would delete."""
    if record is None or record.get("setup_completed") is True:
        return "no_unfinished_setup", None
    marker = record.get(setup_progress.CREATED_BY_APP)
    if (
        not isinstance(marker, dict)
        or marker.get("user_id") != user_id
        or marker.get("workspace") != str(config.WORKSPACE)
    ):
        return "not_created_by_app", None
    if [row["id"] for row in db.execute("SELECT id FROM users")] != [user_id]:
        return "other_accounts", None
    agent = get_corsia_agent(db=db)
    if agent is None or [row["id"] for row in db.execute("SELECT id FROM threads")] != [agent]:
        return "in_use", None
    if not _only_held_inputs(db, agent):
        return "in_use", None
    return None, agent


def _remove_unchanged_files(workspace: Path, files: object) -> None:
    """Delete each file setup created, only while it is a plain file still matching the digest recorded for it."""
    if not isinstance(files, dict):
        return
    root = workspace.resolve()
    for relative, digest in files.items():
        path = workspace / str(relative)
        try:
            if path.is_symlink() or not path.is_file() or not path.resolve().is_relative_to(root):
                continue
            if hashlib.sha256(path.read_bytes()).hexdigest() == digest:
                path.unlink()
        except OSError:
            logger.exception("Could not remove a file the unfinished setup created")


def _default_work_folder_if_changed() -> str | None:
    default = Path.home() / DEFAULT_WORK_FOLDER
    try:
        if config.WORKSPACE.resolve() == default.resolve():
            return None
    except OSError:
        pass
    return str(default)


def reset_unfinished_setup(user_id: str) -> ResetResult:
    """Reset the unfinished setup ``user_id`` is signed into, or refuse it whole and change nothing."""
    from core.thread_family import delete_thread_family, thread_family_ids
    from plans import public

    with write_transaction(None, scope="setup_reset") as db:
        record = setup_progress.read(db=db)
        refusal, agent = _guard(db, record, user_id)
        if refusal is not None or agent is None or record is None:
            return _refused(refusal or "in_use")
        files = record[setup_progress.CREATED_BY_APP].get("files")
        with public.collect_wake_recipients(db, None):
            delete_thread_family(db, thread_family_ids(db, agent))
        db.execute("DELETE FROM auth_tokens WHERE user_id = ?", (user_id,))
        db.execute("DELETE FROM users WHERE id = ?", (user_id,))
        db.execute(
            "DELETE FROM settings WHERE key IN (?, ?)", (CORSIA_AGENT_KEY, setup_progress.KEY),
        )
    _remove_unchanged_files(config.WORKSPACE, files)
    return ResetResult(reset=True, default_work_folder=_default_work_folder_if_changed())
