"""Who and why for a guidance commit: the saver's Git author and the sanitized trailers (issue #2272).

:mod:`infra.guidance_git` owns the Git mechanics; this module only composes the text it commits
and the lines a save reports to its caller.
"""

from __future__ import annotations

import re
from contextlib import closing

from loguru import logger

from host_access.credential_sanitizer import contains_credential

_CORSIA = "CORSIA <corsia@localhost>"
_SAFE_RE = re.compile(r"[^\w.-]")
_EVIDENCE_RE = re.compile(r"https?://\S+|[A-Za-z0-9][\w.:/#+-]{0,119}")

SAVE_COMMITTED = "Saved and committed {sha} ({scope}). Reason recorded."
SAVE_UNRECORDED = "Saved and committed {sha} with Reason: unrecorded. Pass reason on your next guidance save so the history says why."
SAVE_UNSYNCED = ("Partly saved: commit {sha} landed, but Git's index still holds the old {paths} ({error}). Run "
                 "`{command}` before your next commit, or that commit will undo this change. Report this.")
SAVE_FAILED = "Saved, but the commit failed: {error}. The change is on disk and not in history. Report this; do not claim it is recorded."
SAVE_HISTORY_OFF = "Saved. History is off: {why}."
SAVE_STALE = "Error: {path} changed since this save read it; nothing was written. Re-read it and retry."


def _clean(value: str) -> str:
    return " ".join(value.split()).replace("<", "(").replace(">", ")")


def saver_author(*, thread_id: str | None = None, user_id: str | None = None) -> str:
    """The Git author of a save: the user, the saving thread by its title, or CORSIA."""
    if user_id:
        return f"{_clean(user_id) or 'user'} <{_SAFE_RE.sub('-', user_id)}@users.corsia.local>"
    if not thread_id:
        return _CORSIA
    name = thread_id
    try:
        from infra.database import get_db

        with closing(get_db()) as db:
            name = (db.execute("SELECT title FROM threads WHERE id = ?", (thread_id,)).fetchone() or {"title": None})["title"] or name
    except Exception as exc:  # the title is cosmetic; the thread id still identifies the saver
        logger.warning("thread title lookup failed for {}: {}", thread_id, exc)
    return f"{_clean(name) or thread_id} <{thread_id}@agents.corsia.local>"


def is_person_author(author: str | None) -> bool:
    """Whether a guidance author is the person: only a user-route save names a user address."""
    return author is not None and author.endswith("@users.corsia.local>")


def recorded_reason(reason: str | None) -> str:
    """One line of at most 200 characters, or empty when absent or credential-shaped."""
    line = (reason or "").strip().split("\n")[0].strip()[:200]
    return "" if contains_credential(line) else line


def commit_message(subject: str, scope: str, reason: str | None, evidence: str | None) -> str:
    """Subject, any sanitizing notes, then the trailers (which must come last)."""
    notes = ["Reason dropped: it looked like a credential."] if (reason or "").strip() and not recorded_reason(reason) else []
    tokens = (evidence or "").replace(",", " ").split()
    kept = [
        token for token in tokens
        if _EVIDENCE_RE.fullmatch(token) and (token.startswith("http") or any(c.isdigit() for c in token))
        and not contains_credential(token)
    ]
    notes += ["Evidence trimmed to identifiers and URLs."] if len(kept) < len(tokens) else []
    trailers = [f"Reason: {recorded_reason(reason) or 'unrecorded'}", *([f"Evidence: {' '.join(kept)}"] if kept else []), f"Scope: {scope}"]
    return "\n\n".join([subject, *([" ".join(notes)] if notes else []), "\n".join(trailers)])
