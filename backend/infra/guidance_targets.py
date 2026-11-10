"""History path classification and its stable target value: guidance first, then documents."""

from __future__ import annotations

from contextlib import closing
from dataclasses import dataclass
from pathlib import Path

from loguru import logger

DOCUMENT_SCOPE = "document"


@dataclass(frozen=True)
class HistoryTarget:
    """A history save's scope label, or the fault that hid it.

    ``document`` marks an ordinary document: recorded by the same writer, but none of the
    guidance-only work (the lesson check, the carry-in record) follows its save. ``registered``
    marks a registered thread-folder document, which a line ignoring whole thread trays does not
    keep out of history (:mod:`infra.history_ignore`)."""

    scope: str
    fault: str | None = None
    document: bool = False
    registered: bool = False

    @property
    def kind(self) -> str:
        """What its log line, product event and commit subjects call it: ``document`` or ``guidance``."""
        return "document" if self.document else "guidance"


def guidance_target(path: Path, workspace: Path | None = None) -> HistoryTarget | None:
    """Return a guidance target, preserving classification faults for the caller."""
    from core.guidance_paths import classify_guidance_path
    from infra.config import WORKSPACE
    from infra.database import get_db

    try:
        with closing(get_db()) as db:
            scope = classify_guidance_path(path, workspace=workspace or Path(WORKSPACE), db=db)
    except Exception as exc:
        logger.warning("guidance classification failed for {}: {}", path, exc)
        return HistoryTarget("unknown", f"could not check whether this file is guidance ({exc})")
    return HistoryTarget(scope) if scope else None


def history_target(path: Path, workspace: Path | None = None) -> HistoryTarget | None:
    """The target whose saves the workspace history records: guidance first, else a recorded document.

    A classification that fails is a target carrying its fault, as guidance's is, so the save and the
    History control both say history may be missing. One the workspace's Git ignores is a target, and
    the writer's one ignore gate leaves it unrecorded."""
    target = guidance_target(path, workspace)
    if target is not None:
        return target
    from infra.config import WORKSPACE

    root = Path(workspace or WORKSPACE)
    return document_target(path, root)


def document_target(path: Path, workspace: Path) -> HistoryTarget | None:
    """The document half of the decision (no Git); a failed check is a document target with its fault."""
    from core.document_history_paths import in_thread_folder, is_history_document
    from infra.database import get_db

    try:
        with closing(get_db()) as db:
            if not is_history_document(path, workspace=workspace, db=db):
                return None
    except Exception as exc:
        logger.warning("document history classification failed for {}: {}", path, exc)
        return HistoryTarget(DOCUMENT_SCOPE, f"could not check whether this file has history ({exc})", document=True)
    return HistoryTarget(DOCUMENT_SCOPE, document=True, registered=in_thread_folder(path, workspace))
