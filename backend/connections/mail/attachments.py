"""Workspace attachment validation for reviewed email drafts."""
from __future__ import annotations

import hashlib
import mimetypes
from email.message import EmailMessage
from pathlib import Path
from typing import Any

from connections.mail.email_document import DraftRefused
from tools.filesystem_gate import _resolve_path
from tools.reviewed_drafts import _workspace_root


def inspect(paths: Any, limit: int | None) -> list[dict[str, Any]]:
    if paths is None:
        return []
    if not isinstance(paths, list) or not all(isinstance(item, str) and item.strip() for item in paths):
        raise DraftRefused("Attachments must be a list of workspace file paths.")
    root = _workspace_root().resolve()
    entries: list[dict[str, Any]] = []
    total = 0
    for value in paths:
        try:
            path = _resolve_path(value, root)
            if not path.is_relative_to(root) or not path.is_file():
                raise OSError
            data = path.read_bytes()
        except (OSError, PermissionError, ValueError):
            raise DraftRefused(f"Attachment {Path(value).name} is missing or unreadable in the workspace.") from None
        total += len(data)
        entries.append({"path": str(path.relative_to(root)), "name": path.name, "size": len(data),
                        "sha256": hashlib.sha256(data).hexdigest()})
    if limit is not None and total > limit:
        raise DraftRefused("The attachments exceed this provider's size limit.")
    return entries


def add(message: EmailMessage, entries: list[dict[str, Any]]) -> None:
    root = _workspace_root().resolve()
    for entry in entries:
        try:
            path = _resolve_path(str(entry["path"]), root)
            if not path.is_relative_to(root):
                raise OSError
            data = path.read_bytes()
        except (OSError, PermissionError, ValueError, KeyError):
            raise DraftRefused("An attachment is missing or unreadable. Save the draft again.") from None
        if hashlib.sha256(data).hexdigest() != entry.get("sha256"):
            raise DraftRefused(f"Attachment {path.name} changed. Save the draft again and show the person.")
        mime = mimetypes.guess_type(path.name)[0] or "application/octet-stream"
        maintype, subtype = mime.split("/", 1)
        message.add_attachment(data, maintype=maintype, subtype=subtype, filename=path.name)
