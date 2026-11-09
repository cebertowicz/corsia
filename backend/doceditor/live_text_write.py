"""The live lane's file write: a recorded document is saved as a version by its actor (C11)."""

from __future__ import annotations

import logging
from pathlib import Path

from doceditor import document_save
from doceditor.document_save import DocumentSaveError, FileChangedOutside
from doceditor.document_scope import UI_THREAD
from infra import config
from infra.guidance_git import save_guidance
from infra.guidance_messages import saver_author
from infra.guidance_targets import history_target

_log = logging.getLogger(__name__)


def write_live_text(path: Path, text: str, prior_sha256: str, actor=None, reason: str | None = None) -> None:
    """Blocking. The live lane's write: a recorded document is saved as a version by its actor through the
    history writer, any other file as before; a changed file is never replaced (``FileChangedOutside``)."""
    target = history_target(path, Path(config.WORKSPACE))
    if target is None or not target.document:
        document_save.write_atomic_plain(path, text, prior_sha256)
        return
    saved = save_guidance(
        {path: text.encode("utf-8")}, workspace=Path(config.WORKSPACE), target=target, author=_live_author(actor),
        reason=reason, expected={path: prior_sha256},
        write=lambda _path, data: document_save.write_atomic_plain(path, data.decode("utf-8"), prior_sha256),
    )
    if saved.outcome == "stale":
        raise FileChangedOutside()
    if saved.outcome == "refused":
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
