"""Transcript activity for tool-call rows: the phrase and the object it names.

A catalog tool's verb is its ``ToolCatalogEntry.activity``; any other tool,
including a harness builtin, gets its humanized name. ``tools.tool_display``
is the one caller that turns a call into its row.
"""

from __future__ import annotations

import json
import re
from typing import NamedTuple

ACTIVITY_OBJECT_ARGS = (
    "file_path", "path", "doc_path", "notebook_path", "description",
    "command", "pattern", "query", "url", "title", "name", "thread_id",
    "artifact_id", "external_id",
)
_PATH_ARGS = frozenset({"file_path", "path", "doc_path", "notebook_path"})
_CD_PREFIX = re.compile(r"^\s*(?:cd\s+(?:'(?:\\.|[^'])*'|\"(?:\\.|[^\"])*\"|(?:\\.|[^;&\s])+)\s*(?:&&|;)\s*)+")


class ToolActivity(NamedTuple):
    """A call's row label and the one argument value it names, unshortened."""

    phrase: str
    object: str


def _humanize_tool_name(name: str) -> str:
    """Return the visible fallback for a tool from outside CORSIA's catalog."""
    words = [word for word in name.replace("-", "_").split("_") if word]
    if not words:
        return "Using tool"
    verbs = {
        "read": "Reading", "write": "Writing", "edit": "Editing",
        "update": "Updating", "create": "Creating", "list": "Listing",
        "search": "Searching", "fetch": "Fetching", "open": "Opening",
        "close": "Closing", "click": "Clicking", "fill": "Filling",
        "wait": "Waiting", "cancel": "Cancelling", "send": "Sending",
    }
    action = words[-1].lower()
    subject = " ".join(words[:-1])
    text = f"{verbs[action]} {subject}".strip() if action in verbs else " ".join(words)
    return text[:1].upper() + text[1:]


def _activity_argument(arguments: object) -> tuple[str, str]:
    """Select the one existing argument a row names, as ``(key, value)``."""
    if not isinstance(arguments, dict):
        return "", ""
    for key in ACTIVITY_OBJECT_ARGS:
        value = arguments.get(key)
        if isinstance(value, str) and value:
            return key, value
    return "", ""


def _display_command(command: str) -> str:
    """Hide leading shell directory setup from a collapsed activity phrase only."""
    return _CD_PREFIX.sub("", command)


def _skill_label(path: str) -> str | None:
    """The skill a path targets, for ``…/skills/<name>/SKILL.md`` or a reference file.

    The row names the skill package instead of the repeated ``SKILL.md``
    basename; a private path prefix never reaches the label.
    """
    parts = [part for part in path.replace("\\", "/").rstrip("/").split("/") if part]
    for index in range(len(parts) - 3, -1, -1):
        if parts[index] != "skills":
            continue
        name = parts[index + 1]
        rest = parts[index + 2:]
        if len(rest) == 1 and rest[0] == "SKILL.md":
            return f"{name} skill"
        if len(rest) > 1 and rest[0] == "references":
            return f"{name} skill (references/{'/'.join(rest[1:])})"
    return None


def tool_activity(name: str | None, arguments: object) -> ToolActivity:
    """Render the activity for one already-resolved tool name and arguments."""
    from tools.catalog import TOOL_CATALOG, normalize_tool_name

    canonical = normalize_tool_name(name)
    entry = TOOL_CATALOG.get(canonical)
    verb = entry.activity if entry else _humanize_tool_name(canonical)
    key, value = _activity_argument(arguments)
    shown = value.rstrip("/").rsplit("/", 1)[-1] or value if key in _PATH_ARGS else value
    if key in _PATH_ARGS:
        shown = _skill_label(value) or shown
    if key == "command" and verb == "Running":
        shown = _display_command(shown)
    return ToolActivity(f"{verb} {shown}".strip(), value)


def resolve_display_call(name: str | None, arguments: object) -> tuple[str, object]:
    """The tool a call really runs and its arguments, looking through a ``tool_call`` bridge.

    Arguments may arrive as a JSON string or a partial stream chunk; this is the one place they are read.
    """
    from tools.tool_surface import resolve_dispatch

    if isinstance(arguments, str):
        try:
            arguments = json.loads(arguments) if arguments else {}
        except ValueError:
            arguments = {}
    resolved_name, resolved_arguments, _ = resolve_dispatch(name or "", arguments)
    return resolved_name, resolved_arguments
