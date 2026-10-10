"""Read and resolve the ``**Owner:**`` and ``**Review:**`` status fields of a shared file."""

from __future__ import annotations

from itertools import islice
from pathlib import Path
import re

from registry.shared_principals import UI_PRINCIPAL, resolve_explicit_owner, scope_conductor

_STATUS_FIELD = re.compile(
    r"\*\*(Owner|Review):\*\*\s*(.*?)(?=\s+·\s+|$)",
    re.IGNORECASE | re.MULTILINE,
)
# Headers are hand-written: "Lane (id)", "Lane (`id`)", "Lane (`id`); worker `w`",
# "Lane b96b3847", "Lane. Other lane accepts ...". IDs are tried first, then the leading name.
_ID_TOKEN = re.compile(r"[(`]\s*`?([^()`\s;,]+)`?\s*[)`]|\b([0-9a-f]{8})\b")
_LEADING_NAME = re.compile(r"^\s*(.+?)\s*(?:[(`;,]|\.(?:\s|$)|$)")
_TEXT_SUFFIXES = {".md", ".txt"}
_HEADER_LINES = 10


def header_fields(path: Path) -> dict[str, str]:
    """Read status fields from only the first few lines of supported text files."""
    if path.suffix.casefold() not in _TEXT_SUFFIXES:
        return {}
    try:
        with path.open("r", encoding="utf-8") as handle:
            text = "".join(islice(handle, _HEADER_LINES))
    except (OSError, UnicodeError):
        return {}
    return {
        match.group(1).casefold(): match.group(2).strip()
        for match in _STATUS_FIELD.finditer(text)
    }


def _scope_user_id(db, project_id: str | None) -> str | None:
    """Resolve the user who owns the scope through its designated conductor."""
    conductor = scope_conductor(project_id, db=db)
    if conductor is None:
        return None
    row = db.execute("SELECT user_id FROM threads WHERE id=?", (conductor,)).fetchone()
    return str(row["user_id"]) if row else None


def _resolve(candidate: str, project_id: str | None, user_id: str, db) -> str | None:
    if candidate.casefold() in {user_id.casefold(), *user_id.casefold().split()[:1]}:
        return UI_PRINCIPAL
    try:
        return resolve_explicit_owner(candidate, project_id=project_id, user_id=user_id, db=db)
    except ValueError:
        return None


def resolve_header_owner(value: str, project_id: str | None, db) -> str | None:
    """Return the one principal a header names, or ``None`` when it names none or several."""
    user_id = _scope_user_id(db, project_id)
    if user_id is None:
        return None
    by_id = {
        owner
        for match in _ID_TOKEN.finditer(value)
        if (owner := _resolve(match.group(1) or match.group(2), project_id, user_id, db))
        is not None
    }
    if len(by_id) == 1:
        return by_id.pop()
    if by_id:
        return None
    leading = _LEADING_NAME.match(value)
    names = [value.strip()] + ([leading.group(1)] if leading else [])
    for name in names:
        owner = _resolve(name, project_id, user_id, db)
        if owner is not None:
            return owner
    return None
