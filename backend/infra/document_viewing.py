"""Resolve a human's canvas descriptor once, while admitting their message."""
from __future__ import annotations

import logging
from dataclasses import dataclass
from pathlib import Path

from markdown_it import MarkdownIt

from core.viewing_context import VIEWING_PATH_CAP, safe_viewing_field
from doceditor.doc_text import read_doc_text
import infra.config as config
from infra.auth import may_access_artifact
from registry.models import get_artifact

logger = logging.getLogger(__name__)

#: How much of the anchor line is compared between the editor and the saved file.
_SHOWN_LINE_CAP = 200


class _RootsUnreadable(RuntimeError):
    """The registered roots could not be read, so the path form is unknown."""


@dataclass(frozen=True, slots=True)
class PreparedViewingDocument:
    """Read-side facts; admission only selects these or the prior frozen note."""

    target: dict
    note: dict | None
    #: How a fresh resolution ended, for the product event. None on a retry,
    #: which reuses its stored note and resolves nothing.
    outcome: str | None = None


@dataclass(frozen=True, slots=True)
class ViewingDocumentSource:
    """Authorized registry path and anchor; no filesystem work on the DB worker."""

    target: dict
    path: str
    #: The anchor line as the editor showed it. Compared, never stored.
    shown_line: str | None = None


def viewing_document_target(value: object) -> dict | None:
    """Client text is never accepted; retain identity only for idempotent admission."""
    if not isinstance(value, dict) or not isinstance(value.get("artifact_id"), str):
        return None
    artifact_id = value["artifact_id"].strip()
    if not artifact_id:
        return None
    target: dict = {"artifact_id": artifact_id}
    anchor = value.get("heading_anchor")
    if type(anchor) is int and anchor > 0:
        target["heading_anchor"] = anchor
    return target


def _comparable(line: str) -> str:
    """One sanitizer for both sides, the note's own, comparing at most the cap.

    The editor sends a line only when it is no longer than the cap, so its
    lines are compared whole. Any other sender's longer line matches on its
    first ``_SHOWN_LINE_CAP`` characters; that can only keep the server's own
    heading, never supply one.
    """
    return safe_viewing_field(line, _SHOWN_LINE_CAP)


def viewing_shown_line(value: object) -> str | None:
    """Return the anchor line the client's editor showed, for comparison only.

    It can remove a heading from the note, never supply one: the stored
    heading is always the server's own reading of the saved file. A sender
    that gives no text here gets no heading, since nothing was compared.
    """
    line = value.get("heading_line") if isinstance(value, dict) else None
    return _comparable(line) if isinstance(line, str) else None


def _heading_at_line(path: str, anchor: int | None, shown_line: str | None) -> str:
    """Return the heading that starts on source line ``anchor`` of the saved file.

    The anchor comes from the editor and is read against the file on disk. The
    two can differ (an unsaved edit, a write since the document loaded), and
    the same line number may then hold another heading. So the saved line must
    match what the editor showed on that line, or the note names the path
    alone. There is no uncompared path: an anchor without that text names no
    heading.
    """
    if anchor is None or shown_line is None:
        return ""
    try:
        text = read_doc_text(path)
    except (OSError, ValueError, UnicodeError) as exc:
        # Too large or not text: the note keeps the path and names no section.
        logger.info("viewing note has no section, document unreadable: %s", exc)
        return ""
    lines = text.splitlines(keepends=True)
    if anchor > len(lines) or _comparable(lines[anchor - 1]) != shown_line:
        logger.info("viewing note has no section, saved file differs from the editor")
        return ""
    # Include the following line for a setext underline, preserving fence context.
    prefix = "".join(lines[:anchor + 1])
    tokens = MarkdownIt().parse(prefix)
    for index, token in enumerate(tokens):
        if token.type == "heading_open" and token.map and token.map[0] + 1 == anchor:
            return safe_viewing_field(tokens[index + 1].content, 80)
    return ""


def resolve_viewing_source(
    db, thread_id: str | None, target: dict, shown_line: str | None = None,
) -> ViewingDocumentSource | None:
    """Check ownership on the read connection, then release it before reading disk."""
    if not thread_id:
        return None
    thread = db.execute("SELECT user_id FROM threads WHERE id=?", (thread_id,)).fetchone()
    artifact = get_artifact(target["artifact_id"], db=db)
    if (thread is None or artifact is None or artifact["state"] != "active"
            or not artifact.get("path")
            or not may_access_artifact(artifact["parent_thread_id"], thread["user_id"], db=db)):
        return None
    return ViewingDocumentSource(target, artifact["path"], shown_line)


def _path_for_agent(path: Path, workspace: Path) -> str | None:
    """Return the path the note names, or None when the file may not be named.

    Workspace files are named relative to the workspace. A file in a registered
    repository outside it is named by its absolute path, the form agents
    already get for such files and can open. Registration uses the same test.

    This runs in the disk phase on purpose. Whether the reader may see the
    document was settled on the read connection in ``resolve_viewing_source``;
    this decides only which form of the path to name. The registered-root test
    resolves links and checks each repository's binding on disk, which must
    not run on the database worker, and it reads its policies on a connection
    of its own. If those cannot be read it raises ``_RootsUnreadable``: that is
    not the answer "outside every allowed root" and is reported apart from it.
    """
    if path.is_relative_to(workspace):
        return str(path.relative_to(workspace))
    # Imported here: registry.service imports the tools layer.
    from infra.project_repo_policy import RepoPolicyQueryError
    from registry.service import registrable_zone

    try:
        return str(path) if registrable_zone(path) is not None else None
    except RepoPolicyQueryError as exc:
        raise _RootsUnreadable(str(exc)) from exc


def materialize_viewing_document(
    source: ViewingDocumentSource | PreparedViewingDocument | None,
) -> PreparedViewingDocument | None:
    """Read and parse off the DB pool; retries already carry their frozen note."""
    if not isinstance(source, ViewingDocumentSource):
        return source
    workspace = config.WORKSPACE.resolve()
    path = Path(source.path).expanduser()
    path = (path if path.is_absolute() else workspace / path).resolve()
    if not path.is_file():
        return PreparedViewingDocument(source.target, None, "unresolved")
    try:
        named = _path_for_agent(path, workspace)
    except _RootsUnreadable as exc:
        # Not a policy answer: the message is still admitted, and the event says why no note.
        logger.warning("viewing note omitted, registered roots unreadable: %s", exc)
        return PreparedViewingDocument(source.target, None, "roots_unreadable")
    if named is None:
        return PreparedViewingDocument(source.target, None, "outside_allowed")
    note = {
        "path": safe_viewing_field(named, VIEWING_PATH_CAP, keep_filename=True),
        "heading": _heading_at_line(
            str(path), source.target.get("heading_anchor"), source.shown_line,
        ),
    }
    return PreparedViewingDocument(
        source.target, note, "section" if note["heading"] else "path_only",
    )
