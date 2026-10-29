"""The outside-content label at agent delivery and deterministic handoff boundaries."""
from collections.abc import Mapping
from typing import Any

from core.injection_rules import INJECTION_RULES

OUTSIDE_CONTENT_LABEL = "from an untrusted agent; contains outside content"


def injection_sign_text(facts: Mapping[str, Any]) -> str | None:
    """Describe outside-content signs for delivery without changing their facts."""
    signs = facts.get("injection_signs")
    if facts.get("source") != "outside" or not isinstance(signs, list) or not signs:
        return None
    rules = dict(INJECTION_RULES)
    return ", ".join(f"{sign} ({rules[sign]})" if sign in rules else str(sign) for sign in signs)


def labelled_summary(text: str, facts: Mapping[str, Any]) -> str:
    return f"{text} — {OUTSIDE_CONTENT_LABEL}" if facts.get("source") == "outside" else text


def labelled_event(event: dict[str, Any]) -> dict[str, Any]:
    """Label the envelope, leaving facts and model-written payload as data."""
    if event["facts"].get("source") == "outside":
        return {**event, "content_label": OUTSIDE_CONTENT_LABEL}
    return event
