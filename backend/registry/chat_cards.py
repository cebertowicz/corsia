"""The artifact cards a chat message carries: how one card reads, live.

Two sources feed a card (``api/thread_cards.py`` chooses the message):

- an **ask card** for one user ask, identified by its hold. The hold is the only
  lifecycle owner: whether the round is open or how it ended is read from the
  hold and its clearance event each time, so nothing here is stored or copied.
- a **reference card** for an accessible registered artifact or scoped file named
  in a message (``registry/path_cards.py``).

A card carries what the tile shows (title, type, file name, state) and what
opening it needs (an artifact id or, for an unregistered file, its path).
Registered titles and states reflect the registry's current truth.
"""

from __future__ import annotations

import json
from collections.abc import Mapping
from pathlib import PurePosixPath
from typing import Any

from infra.auth import may_access_artifact
from registry import models as registry_models
from registry.presentation import CAPABILITY_LEAF_PACKAGE, resolve_descriptor

#: What the review bar will record in a clearance event for a round the user
#: answered (review bar D3). Nothing writes it yet, so until it lands a cleared
#: round reads as answered or replaced.
VERDICTS = frozenset({"approved", "approved_with_changes", "changes_requested"})
#: An open ask, a round answered without a verdict, one the user replaced, and
#: one whose artifact is gone.
NEEDS_REVIEW = "needs_review"
ANSWERED = "answered"
REPLACED = "replaced"
UNAVAILABLE = "unavailable"

UNAVAILABLE_TITLE = "No longer available"


def _payload(raw: object) -> dict:
    try:
        parsed = json.loads(str(raw)) if raw else {}
    except (TypeError, ValueError):
        return {}
    return parsed if isinstance(parsed, dict) else {}


def _file_name(artifact: Mapping[str, Any]) -> str | None:
    raw = artifact.get("path") or artifact.get("external_id")
    return PurePosixPath(str(raw)).name or None if raw else None


def _counts(raw: object) -> dict[str, int] | None:
    if not isinstance(raw, dict):
        return None
    counts = {
        key: int(raw[key]) for key in ("comments", "suggestions", "edits")
        if isinstance(raw.get(key), int) and raw[key] > 0
    }
    return counts or None


def _presentation(
    row: Mapping[str, Any] | None, artifact_id: str | None, kind: str, available: bool,
) -> dict[str, Any]:
    title = (row.get("title") or _file_name(row)) if row is not None else None
    shown = resolve_descriptor(row.get("integration"), row.get("type")) if row is not None else None
    return {
        "kind": kind,
        "artifact_id": artifact_id,
        "title": title or UNAVAILABLE_TITLE,
        "type": row.get("type") if row is not None else None,
        "icon": shown.icon if shown else "file-question",
        "name": _file_name(row) if row is not None else None,
        "path": (row.get("path") or row.get("external_id")) if row is not None else None,
        "available": available,
        # Only a file the artifact pane opens in the canvas is a button.
        "opens": available and shown is not None and shown.capability == CAPABILITY_LEAF_PACKAGE,
    }


def file_card(path: str, file_type: str) -> dict[str, Any]:
    """Present a file already verified to exist within scope."""
    return _presentation({"path": path, "type": file_type, "integration": "workspace"}, None, "artifact", True)


def card_for(
    artifact: Mapping[str, Any] | None, artifact_id: str | None, *, kind: str,
    hold_id: str | None = None, state: str | None = None,
    feedback: dict[str, int] | None = None,
) -> dict[str, Any]:
    """One registry card in its wire shape, with availability from its state."""
    retired = artifact is None or artifact.get("state") not in ("active", "archived")
    card = _presentation(artifact, artifact_id, kind, not retired)
    if kind == "ask":
        card["hold_id"] = hold_id
        card["state"] = UNAVAILABLE if retired else state
        if feedback and not retired:
            card["feedback"] = feedback
    elif retired:
        card["state"] = UNAVAILABLE
    return card


def ask_state(
    hold: Mapping[str, Any], artifact: Mapping[str, Any] | None = None,
) -> tuple[str, dict[str, int] | None]:
    """The round state one ask reads as, with the feedback counts its verdict carried.

    ``hold`` is one ``plans.public.ask_holds`` entry. Open reads as needing
    review. A cleared ask reads its terminal path from the hold's clearance
    event, else its verdict when the review bar recorded one, else answered when
    the person answered. A clearance that recorded none of these predates
    recorded endings: a person's 'replaced' clear then wrote its outcome only to
    the artifact's attention metadata, so that is read as replaced and anything
    else as answered.
    """
    from plans.public import ASK_ENDINGS

    if hold["open"]:
        return NEEDS_REVIEW, None
    answer = hold["answer"]
    ended = answer.get("ended")
    if ended == "retired":
        return UNAVAILABLE, None
    if ended in ASK_ENDINGS:
        return ended, None
    verdict = answer.get("verdict")
    if verdict in VERDICTS:
        return verdict, _counts(answer.get("feedback_counts"))
    if answer.get("answered_by"):
        return ANSWERED, None
    attention = _payload(artifact.get("metadata")) if artifact else {}
    if attention.get("attention_state") == "cleared" and attention.get("attention_outcome") == "supersede":
        return REPLACED, None
    return ANSWERED, None


def ask_cards(db, asks: Mapping[str, str]) -> dict[str, dict[str, Any]]:
    """Cards for user asks, by hold id: ``{hold_id: artifact_id}`` in, live state out."""
    from plans import public

    holds = public.ask_holds(db, asks)
    cards: dict[str, dict[str, Any]] = {}
    for hold_id, artifact_id in asks.items():
        hold = holds.get(hold_id)
        if hold is None:
            continue
        artifact = registry_models.get_artifact(artifact_id, db=db)
        state, feedback = ask_state(hold, artifact)
        cards[hold_id] = card_for(
            artifact, artifact_id, kind="ask", hold_id=hold_id, state=state, feedback=feedback,
        )
    return cards


def reference_card(db, artifact: Mapping[str, Any], user_id: str) -> dict[str, Any] | None:
    """A card for a registered artifact a message names, only when this user may open it."""
    if not may_access_artifact(artifact.get("parent_thread_id"), user_id, db=db):
        return None
    return card_for(artifact, str(artifact["id"]), kind="artifact")
