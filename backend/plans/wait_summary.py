"""One external-wait summary shared by plan projections."""

from __future__ import annotations

from urllib.parse import urlparse

from plans import node_waits, operator_scheduled
from plans.not_moving import parse_deadline


def from_nodes(nodes: list[dict]) -> dict | None:
    """Return the earliest outside wait, or ``None`` for idle/set-aside work."""
    return _earliest(
        (cause, node)
        for node in nodes
        if not node.get("container") and node["execution"]["condition"] == "waiting"
        for cause in node["execution"]["causes"]
    )


def set_aside_until(nodes: list[dict]) -> str | None:
    """When the earliest work the agent set aside itself comes back (EW7's set-aside row)."""
    ends = [
        parsed
        for node in nodes
        if not node.get("container") and node["execution"]["condition"] == "waiting"
        for cause in node["execution"]["causes"]
        if operator_scheduled.is_set_aside(cause) and cause.get("ends_at")
        if (parsed := parse_deadline(str(cause["ends_at"]))) is not None
    ]
    return min(ends, default=(None, None))[1]


def for_plans(db, plan_ids) -> dict[str, dict]:
    """Each listed plan's wait (EW4: a parent's waiting child plans), in two reads."""
    found = {}
    for plan_id, rows in node_waits.armed_in_plans(db, plan_ids).items():
        if wait := _earliest((node_waits.named_watch_cause(row), {}) for row in rows):
            found[plan_id] = wait
    return found


def _earliest(pairs) -> dict | None:
    waits = [
        (parsed[0], cause, node)
        for cause, node in pairs
        if cause.get("kind") == "waiting" and cause.get("ends_at")
        and not operator_scheduled.is_set_aside(cause)
        if (parsed := parse_deadline(str(cause["ends_at"]))) is not None
    ]
    if not waits:
        return None
    _until, cause, node = min(waits, key=lambda item: item[0])
    summary = {"label": _label(cause, node), "until": cause["ends_at"], "more": len(waits) - 1}
    if cause.get("artifact_id"):
        summary["artifact_id"] = cause["artifact_id"]
    return summary


def _label(cause: dict, node: dict) -> str:
    reason = _stripped_reason(cause.get("blocked_reason"))
    if reason and not _url(reason):
        return reason
    if cause.get("artifact_title"):
        return str(cause["artifact_title"])
    if reason:
        return _url_label(reason)
    if cause.get("child_thread_id"):
        return f"an agent working on {node.get('title') or 'a task'}"
    return "something outside"


def _stripped_reason(value: object) -> str:
    text = str(value or "").strip()
    lower = text.lower()
    for prefix in ("waiting for", "waiting on", "wait for"):
        if lower.startswith(prefix):
            return text[len(prefix):].lstrip(" :–—-\t")
    return text


def _url(value: str) -> bool:
    parsed = urlparse(value)
    return bool(parsed.scheme and parsed.netloc and not any(char.isspace() for char in value))


def _url_label(value: str) -> str:
    parsed = urlparse(value)
    segment = next((part for part in reversed(parsed.path.split("/")) if part), "")
    return f"{parsed.netloc}{' · ' + segment if segment else ''}"
