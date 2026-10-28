"""Working-root attachment validation for reviewed email drafts.

Call these synchronous helpers off the event loop.
"""
from __future__ import annotations

import hashlib
import mimetypes
from email.message import EmailMessage
from pathlib import Path
from typing import Any

from connections.mail.email_document import DraftRefused
from tools.filesystem_gate import _resolve_path
from tools.reviewed_drafts import _working_root

OVERSIZE = "the attachments are too large for this account. Send fewer or smaller files, or share a link instead."


def inspect(thread_id: str, paths: Any, limit: int | None) -> list[dict[str, Any]]:
    if paths is None:
        return []
    if not isinstance(paths, list) or not all(isinstance(item, str) and item.strip() for item in paths):
        raise DraftRefused("Attachments must be a list of workspace file paths.")
    root = _working_root(thread_id).resolve()
    files: list[tuple[Path, int]] = []
    total = 0
    for value in paths:
        try:
            path = _resolve_path(value, root)
            if not path.is_relative_to(root) or not path.is_file():
                raise OSError
            size = path.stat().st_size
        except (OSError, ValueError):
            raise DraftRefused(f"Attachment {Path(value).name} is missing or unreadable in the workspace.") from None
        total += size
        if limit is not None and total > limit:
            raise DraftRefused(f"Not sent: {OVERSIZE}")
        files.append((path, size))
    entries: list[dict[str, Any]] = []
    for path, size in files:
        try:
            with path.open("rb") as source:
                data = source.read(size + 1)
        except OSError:
            raise DraftRefused(f"Attachment {path.name} is missing or unreadable in the workspace.") from None
        if len(data) != size:
            raise DraftRefused(f"Attachment {path.name} changed. Save the draft again and show the person.")
        entries.append({"path": str(path.relative_to(root)), "name": path.name, "size": size,
                        "sha256": hashlib.sha256(data).hexdigest()})
    return entries


def add(thread_id: str, message: EmailMessage, entries: list[dict[str, Any]]) -> None:
    root = _working_root(thread_id).resolve()
    for entry in entries:
        try:
            path = _resolve_path(str(entry["path"]), root)
            size = entry["size"]
            if not path.is_relative_to(root) or not isinstance(size, int) or size < 0 or not path.is_file():
                raise OSError
            if path.stat().st_size != size:
                raise DraftRefused(f"Attachment {path.name} changed. Save the draft again and show the person.")
            with path.open("rb") as source:
                data = source.read(size + 1)
        except (OSError, ValueError, KeyError, TypeError):
            raise DraftRefused("An attachment is missing or unreadable. Save the draft again.") from None
        if len(data) != size or hashlib.sha256(data).hexdigest() != entry.get("sha256"):
            raise DraftRefused(f"Attachment {path.name} changed. Save the draft again and show the person.")
        mime = mimetypes.guess_type(path.name)[0] or "application/octet-stream"
        maintype, subtype = mime.split("/", 1)
        message.add_attachment(data, maintype=maintype, subtype=subtype, filename=path.name)
