"""The outside-content label at agent delivery and deterministic handoff boundaries."""
from collections.abc import Mapping
from typing import Any

OUTSIDE_CONTENT_LABEL = "from an untrusted agent; contains outside content"


def labelled_summary(text: str, facts: Mapping[str, Any]) -> str:
    return f"{text} — {OUTSIDE_CONTENT_LABEL}" if facts.get("source") == "outside" else text


def labelled_event(event: dict[str, Any]) -> dict[str, Any]:
    """Label the envelope, leaving facts and model-written payload as data."""
    if event["facts"].get("source") == "outside":
        return {**event, "content_label": OUTSIDE_CONTENT_LABEL}
    return event
