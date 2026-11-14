"""The live lane's file write: a recorded document is saved as a version by its actor (C11)."""

from __future__ import annotations

import logging
from pathlib import Path

from doceditor import document_save
from doceditor.document_save import DocumentSaveError, FileChangedOutside
from doceditor.document_scope import UI_THREAD
from infra import config
from infra.guidance_git import save_with_history
from infra.guidance_messages import saver_author
from infra.guidance_targets import history_target

_log = logging.getLogger(__name__)


def write_live_text(path: Path, text: str, prior_sha256: str, operation=None) -> None:
    """Blocking. The live lane's write: a recorded document is written and saved as a version by the
    operation's actor under its reason, under the history lock, before this returns; any other file as
    before. A changed file is never replaced (``FileChangedOutside``). An operation that requires history
    (a restore) raises, with the file left as it was, unless its version is recorded."""
    target = history_target(path, Path(config.WORKSPACE))
    if target is None or not target.document:
        document_save.write_atomic_plain(path, text, prior_sha256)
        return
    required = bool(operation and operation.history_required)
    saved = save_with_history(
        {path: text.encode("utf-8")}, workspace=Path(config.WORKSPACE), target=target,
        author=_live_author(operation.actor if operation else None), reason=operation.reason if operation else None,
        expected={path: prior_sha256}, atomic=required,
        accepted_suggestions=operation.accepted if operation else (),
        write=lambda _path, data: document_save.write_atomic_plain(path, data.decode("utf-8"), prior_sha256),
    )
    if saved.outcome == "stale":
        raise FileChangedOutside()
    if saved.outcome == "refused" or (required and saved.outcome not in ("written", "unchanged")):
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
