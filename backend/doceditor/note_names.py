"""How an agent note names a document and whoever changed it, so every note about one document reads the same."""

from __future__ import annotations

from pathlib import Path
from typing import Any


def document_name(artifact: Any, fallback: str = "the document") -> str:
    """The document's title without its extension, else its path's, else its external id's.

    Plan task titles and agent notes both use this rule; ``fallback`` only fits
    the wording around it when the document has none of the three.
    """
    if artifact is None:
        return fallback
    fields = artifact.keys()  # A registry dict or a sqlite3.Row.
    raw = next((artifact[key] for key in ("title", "path", "external_id") if key in fields and artifact[key]), None)
    return Path(str(raw or "")).stem or fallback


def actor_name(db, actor_thread_id: str | None, user_name: str | None = None) -> str:
    """The agent's thread title, or the user's name when no agent made the change."""
    if actor_thread_id is None:
        return user_name or "The user"
    row = db.execute("SELECT title FROM threads WHERE id=?", (actor_thread_id,)).fetchone()
    return str(row["title"]) if row is not None and row["title"] else "An agent"
