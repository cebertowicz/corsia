"""Render the frozen artifact, plan-status and history-date columns.

Pure functions of stored columns: parse once, render deterministically, read no
state, touch no clock. A NULL column renders nothing at all — it means no
capture ran for that row (pre-change rows) — while a captured empty artifact
set renders ``none``, a captured no-plan snapshot renders ``no plan`` and a
failed capture (stored as ``CAPTURE_FAILED_JSON``) renders ``unavailable``, so
"never captured", "captured, and there was none" and "capture failed" stay
distinguishable.

``render_checkpoint_dates`` (#3196) dates the history a checkpoint covers
against the time it was written, from the frozen boundary time and the capture
time: a checkpoint written long after a thread's last activity used to read as a
fresh instruction, because nothing in it said when its history ended.

Every rendered value occupies exactly one physical line: the stored columns
are machine-written, but a corrupt or hand-edited row must not be able to
forge this module's structure, the same posture ``continuity_render``
takes. An off-contract column degrades to a warning marker plus its raw
bytes rather than raising, because raising here would cost the model its
whole checkpoint body.
"""

from __future__ import annotations

import json
from datetime import datetime, timezone
from typing import Any

ARTIFACT_HEADER = "[CORSIA registered artifacts]"
PLAN_HEADER = "[CORSIA plan status]"
DATES_HEADER = "[CORSIA checkpoint dates]"
_UNKNOWN_WRITE = "checkpoint written at an unknown time"
#: Stored in place of a snapshot whose capture failed at install.
CAPTURE_FAILED_JSON = '{"capture_failed":true}'
_UNAVAILABLE = "unavailable: capture failed at install, read live state with the tools"
_UNREADABLE_MARKER = (
    "[CORSIA warning: snapshot could not be read as its contract; "
    "showing the raw stored snapshot below.]"
)


def render_artifact_snapshot(raw: str | None) -> str:
    """Render the frozen artifact rows, or ``""`` when nothing was captured."""
    if raw is None:
        return ""
    if raw == CAPTURE_FAILED_JSON:
        return f"{ARTIFACT_HEADER}\n{_UNAVAILABLE}"
    try:
        snapshot = json.loads(raw)
        rows = snapshot["rows"]
        omitted = snapshot["omitted_count"]
        if (
            not isinstance(snapshot, dict)
            or not isinstance(rows, list)
            or not isinstance(omitted, int)
            or not all(isinstance(row, dict) for row in rows)
        ):
            raise ValueError("artifact snapshot is off its contract")
    except (TypeError, ValueError, KeyError):
        return _unreadable(ARTIFACT_HEADER, raw)
    if not rows:
        return f"{ARTIFACT_HEADER}\nnone"
    lines = [ARTIFACT_HEADER]
    lines.extend(f"- {_artifact_line(row)}" for row in rows)
    if omitted > 0:
        lines.append(f"{omitted} more — artifact_list(owned=true)")
    return "\n".join(lines)


def render_plan_status_snapshot(raw: str | None) -> str:
    """Render the frozen plan status, or ``""`` when nothing was captured.

    A stored projection is ``plans.status_context.render`` output, delivered
    verbatim under the section header: that module owns what a thread is told
    about its plan at a context boundary, so this one adds no projection or
    wording of its own.
    """
    if raw is None:
        return ""
    if raw == CAPTURE_FAILED_JSON:
        return f"{PLAN_HEADER}\n{_UNAVAILABLE}"
    try:
        status = json.loads(raw)
    except (TypeError, ValueError):
        return _unreadable(PLAN_HEADER, raw)
    if status is None:  # captured, and the thread had no plan
        return f"{PLAN_HEADER}\nno plan"
    if not isinstance(status, dict):
        return _unreadable(PLAN_HEADER, raw)
    return f"{PLAN_HEADER}\n{_one_line(raw)}"


def render_checkpoint_dates(through_at: str | None, captured_at: str | None) -> str:
    """Date a checkpoint's covered history against the time it was written.

    #3196: a checkpoint written long after a thread's last activity was read as
    a fresh instruction, because nothing in it said when its history ended.
    Migration 74 -> 75 backfills rows installed before the column existed; one
    whose boundary message is gone stays NULL and renders nothing.
    """
    if through_at is None:
        return ""
    written = (
        f"checkpoint written {_minute(captured_at)}"
        if captured_at
        else _UNKNOWN_WRITE
    )
    return (
        f"{DATES_HEADER} History ends {_minute(through_at)}; {written}. "
        "Its current-work and next-step statements describe the thread as of "
        "the history's end; they are not newer instructions.\n"
    )


def _artifact_line(row: dict[str, Any]) -> str:
    try:
        artifact_id = row.get("id")
        identity = (
            f"id:{_one_line(str(artifact_id))} "
            if artifact_id is not None else ""
        )
        integration = _one_line(str(row["integration"]))
        artifact_type = _one_line(str(row["type"]))
        if row.get("truncated"):
            return f"{identity}({integration}/{artifact_type}) truncated"
        line = "{}{} ({}/{}) {} {}".format(
            identity, _one_line(str(row["title"])), integration, artifact_type,
            _one_line(str(row["locator"])), "owns" if row["owns"] else "follows",
        )
        node = row.get("origin_plan_node_id")
        if node:
            line += f" node:{_one_line(str(node))}"
        if row.get("trigger"):
            line += f" trigger: {_one_line(str(row['trigger']))}"
        if row.get("freshness") == "overdue":
            line += " overdue"
        elif row.get("freshness") == "never_verified":
            line += " never verified"
        return line
    except (KeyError, TypeError):
        return identity + _one_line(_compact(row))


def _unreadable(header: str, raw: str) -> str:
    return "\n".join((header, _UNREADABLE_MARKER, _one_line(raw)))


def _minute(stored: str) -> str:
    """Render one stored ISO-8601 timestamp as UTC minutes, ``2026-09-20T07:24Z``.

    Stored times are naive UTC with seconds and sometimes a fraction; a reader
    only needs the minute. A value this build cannot parse is shown as stored,
    on one line, because dropping the line would hide when the history ends.
    """
    text = stored.strip()
    iso = text[:-1] + "+00:00" if text.endswith("Z") else text
    try:
        parsed = datetime.fromisoformat(iso)
    except ValueError:
        return _one_line(stored)
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%MZ")


def _one_line(text: str) -> str:
    """Escape line breaks so no stored value can forge this module's lines."""
    return text.replace("\n", "\\n").replace("\r", "\\r")


def _compact(value: Any) -> str:
    return json.dumps(value, separators=(",", ":"), ensure_ascii=False, sort_keys=True)
