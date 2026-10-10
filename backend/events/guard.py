"""Bot / self-author guard for event routing.

An event authored by CORSIA's own bot/reviewer identity (or any GitHub App /
bot account) must NOT re-trigger work — otherwise the reviewer's own PR comment
re-triggers a review, which posts another comment, which re-triggers… the storm
this guard exists to prevent.

Two signals, both checked:

1. **Structural** — the GitHub sender/comment author is a Bot account
   (``user.type == "Bot"`` or login ending in ``[bot]``). GitHub Apps (the
   reviewer posts via one) always carry this.
2. **Configured identity** — a settings-managed allowlist of logins that ARE
   CORSIA's own automation (``event_routing.self_authors``). Covers the case
   where automation posts as the authed *user's* PAT rather than a bot account
   (the user's own automation comments must not loop either).

The guard is conservative: it suppresses ONLY author-identified self/bot events.
A genuine human comment is never suppressed.

Agent-side GitHub writes (e.g. reviewer posts a PR comment via
the GitHub API) MUST post under a ``self_authors``-registered login or a GitHub
Bot/App account. Posting under an unregistered human-looking identity bypasses
both checks and re-opens the suppression loop — the reviewer's comment re-triggers
the review, restarting the storm this guard exists to prevent.
"""

from __future__ import annotations

from typing import Any

from loguru import logger

_SETTINGS_KEY = "event_routing"
_SELF_AUTHORS_FIELD = "self_authors"


def _configured_self_authors() -> set[str]:
    """Logins configured as CORSIA's own automation identity (lowercased).

    Stored under settings ``event_routing.self_authors`` (a list). Empty when
    unset — the structural bot check still catches GitHub App posts.
    """
    try:
        from infra.settings import get_setting

        cfg = get_setting(_SETTINGS_KEY) or {}
        authors = cfg.get(_SELF_AUTHORS_FIELD) or []
        return {str(a).lower() for a in authors if a}
    except Exception as exc:  # noqa: BLE001 — guard must never raise into routing
        logger.warning("event-routing guard: failed to read self_authors: {}", exc)
        return set()


def _event_author(payload: dict[str, Any]) -> dict[str, Any]:
    """The user object that authored the triggering event.

    Comment events carry ``comment.user``; the ``sender`` is the actor for
    PR/check events. Prefer the comment author (it is the thing that would
    loop), fall back to the sender.
    """
    comment = payload.get("comment") or {}
    if comment.get("user"):
        return comment["user"]
    return payload.get("sender") or {}


def is_bot_or_self_authored(payload: dict[str, Any]) -> bool:
    """True when the event was authored by a bot or CORSIA's own identity.

    Used by the router to SUPPRESS the event before it routes — a suppressed
    event never reaches a thread, so the reviewer/bot cannot trigger itself.
    """
    user = _event_author(payload)
    login = str(user.get("login", "")).lower()
    user_type = str(user.get("type", "")).lower()

    if user_type == "bot" or login.endswith("[bot]"):
        return True

    if login and login in _configured_self_authors():
        return True

    return False
