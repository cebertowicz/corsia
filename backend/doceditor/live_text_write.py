"""The live lane's file write: a recorded document is saved as a version by its actor (C11)."""

from __future__ import annotations

import hashlib
import logging
from pathlib import Path

from doceditor import document_save
from doceditor.document_save import DocumentSaveError, FileChangedOutside
from doceditor.document_scope import UI_THREAD
from doceditor.live_store import StoreRefused
from infra import config
from infra.guidance_git import save_with_history
from infra.guidance_messages import saver_author
from infra.guidance_targets import history_target

_log = logging.getLogger(__name__)


class HistoryLockBusy(StoreRefused):
    """A conditional file write can be retried after the history lock clears."""
    code = "history_lock_busy"


def write_live_text(path: Path, text: str, prior_sha256: str, operation=None) -> None:
    """Blocking. The live lane's write: a recorded document is written and saved as a version by the
    operation's actor under its reason, under the history lock, before this returns; any other file as
    before. A change detected before replacement raises ``FileChangedOutside``; an independent writer
    can still race the check and rename. A restore raises unless its version is recorded.

    Text whose bytes equal the file's is not written: a flush with nothing new (the last viewer
    leaving) renames nothing and records no version. The lane still records and publishes it."""
    if hashlib.sha256(text.encode("utf-8")).hexdigest() == prior_sha256:
        from registry.durable_write_preconditions import check_expected_content

        check_expected_content(path, prior_sha256)
        return
    target = history_target(path, Path(config.WORKSPACE))
    if target is None or not target.document:
        document_save.write_atomic_plain(path, text, prior_sha256)
        return
    required = bool(operation and operation.history_required)
    saved = save_with_history(
        {path: text.encode("utf-8")}, workspace=Path(config.WORKSPACE), target=target,
        author=_live_author(operation.actor if operation else None), reason=operation.reason if operation else None,
        expected={path: prior_sha256}, atomic=required,
        accepted_suggestions=operation.accepted if operation else (), require_history_lock=True,
        write=lambda _path, data: document_save.write_atomic_plain(path, data.decode("utf-8"), prior_sha256),
    )
    if saved.reason == "deferred-guidance-lock":
        raise HistoryLockBusy(saved.line or "The document is busy. Try saving again.")
    if saved.outcome == "stale":
        from registry.durable_write_preconditions import check_expected_content

        check_expected_content(path, prior_sha256)
        raise FileChangedOutside()
    if saved.reason == "history-lock-failed" or saved.outcome == "refused" or (required and saved.outcome not in ("written", "unchanged")):
        raise DocumentSaveError("save_failed", saved.line or "Document was not saved")
    if saved.line:
        _log.warning("live document %s saved without its version: %s", path, saved.line)


def _live_author(actor) -> str:
    """The Git author of a live write: the person, the writing agent, or CORSIA for an unattributed one."""
    if actor is None:
        return saver_author()
    if actor.direct_user or actor.thread_id == UI_THREAD:
        return saver_author(user_id=actor.user_id) if actor.user_id else saver_author()
    return saver_author(thread_id=actor.thread_id)
