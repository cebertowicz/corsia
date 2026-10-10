"""The artifact cards a chat message carries: how one card reads, live.

Two sources feed a card (``api/thread_cards.py`` chooses the message):

- an **ask card** for one user ask, identified by its hold. The hold is the only
  lifecycle owner: whether the round is open or how it ended is read from the
  hold and its clearance event each time, so nothing here is stored or copied.
- a **reference card** for a registered artifact a message's text names
  (``registry/path_cards.py``).

A card carries what the tile shows (title, type, file name, state) and what
opening it needs (artifact id). Titles and states are the registry's current
truth, so a renamed or retired artifact reads as it is now.
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


def card_for(
    artifact: Mapping[str, Any] | None, artifact_id: str, *, kind: str,
    hold_id: str | None = None, state: str | None = None,
    feedback: dict[str, int] | None = None,
) -> dict[str, Any]:
    """One card in its wire shape.

    A retired artifact keeps its registry title and reads unavailable; only a row
    that is gone has no title, which the tile words as "No longer available".
    """
    row: Mapping[str, Any] = artifact or {}
    gone = artifact is None
    retired = gone or row.get("state") not in ("active", "archived")
    title = None if gone else (row.get("title") or _file_name(row))
    shown = None if gone else resolve_descriptor(row.get("integration"), row.get("type"))
    card: dict[str, Any] = {
        "kind": kind,
        "artifact_id": artifact_id,
        "title": title or UNAVAILABLE_TITLE,
        "type": None if gone else row.get("type"),
        "icon": shown.icon if shown else "file-question",
        "name": None if gone else _file_name(row),
        "path": None if gone else (row.get("path") or row.get("external_id")),
        "available": not retired,
        # Only a file the artifact pane opens in the canvas is a button.
        "opens": not retired and shown is not None and shown.capability == CAPABILITY_LEAF_PACKAGE,
    }
    if kind == "ask":
        card["hold_id"] = hold_id
        card["state"] = UNAVAILABLE if retired else state
        if feedback and not retired:
            card["feedback"] = feedback
    elif retired:
        card["state"] = UNAVAILABLE
    return card


def ask_state(
    hold: Mapping[str, Any], payload_json: object, artifact: Mapping[str, Any] | None,
) -> tuple[str, dict[str, int] | None]:
    """The round state one hold reads as, with the feedback counts its verdict carried.

    Open reads as needing review. A cleared hold reads as its verdict when the
    review bar recorded one in the clearance event's payload. Without one it
    reads as replaced when the artifact's attention was cleared as superseded,
    and as answered otherwise (a round answered before verdicts were recorded).
    """
    if hold["cleared_at"] is None:
        return NEEDS_REVIEW, None
    payload = _payload(payload_json)
    verdict = payload.get("verdict")
    if verdict in VERDICTS:
        return verdict, _counts(payload.get("feedback_counts"))
    attention = _payload(artifact.get("metadata")) if artifact else {}
    if attention.get("attention_state") == "cleared" and attention.get("attention_outcome") == "supersede":
        return REPLACED, None
    return ANSWERED, None


def ask_cards(db, asks: Mapping[str, str]) -> dict[str, dict[str, Any]]:
    """Cards for user asks, by hold id: ``{hold_id: artifact_id}`` in, live state out."""
    if not asks:
        return {}
    ids = list(asks)
    marks = ",".join("?" * len(ids))
    holds = {
        row["id"]: row for row in db.execute(
            f"SELECT id, cleared_at, clearance_event_id FROM plan_node_holds WHERE id IN ({marks})",
            ids,
        )
    }
    event_ids = [h["clearance_event_id"] for h in holds.values() if h["clearance_event_id"]]
    events = {}
    if event_ids:
        event_marks = ",".join("?" * len(event_ids))
        events = {
            row["id"]: row["payload_json"] for row in db.execute(
                f"SELECT id, payload_json FROM plan_node_events WHERE id IN ({event_marks})",
                event_ids,
            )
        }
    cards: dict[str, dict[str, Any]] = {}
    for hold_id, artifact_id in asks.items():
        hold = holds.get(hold_id)
        if hold is None:
            continue
        artifact = registry_models.get_artifact(artifact_id, db=db)
        state, feedback = ask_state(hold, events.get(hold["clearance_event_id"]), artifact)
        cards[hold_id] = card_for(
            artifact, artifact_id, kind="ask", hold_id=hold_id, state=state, feedback=feedback,
        )
    return cards


def reference_card(db, artifact: Mapping[str, Any], user_id: str) -> dict[str, Any] | None:
    """A card for a registered artifact a message names, only when this user may open it."""
    if not may_access_artifact(artifact.get("parent_thread_id"), user_id, db=db):
        return None
    return card_for(artifact, str(artifact["id"]), kind="artifact")
