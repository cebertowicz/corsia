"""Identify person submits and agent commands that can write a page value."""
from __future__ import annotations

import shlex
from typing import Mapping, Sequence

_NO_VALUE_COMMANDS = frozenset({
    "click", "dblclick", "hover", "focus", "scroll", "scrollintoview", "get", "is", "snapshot", "screenshot",
    "wait", "tab", "open", "goto", "navigate", "back", "forward", "reload",
})


def is_submit(kind: object, event: Mapping[str, object]) -> bool:
    """Whether a pane input is the person's own mouse press or Enter key press."""
    if kind == "input_mouse":
        return event.get("eventType") == "mousePressed"
    return kind == "input_keyboard" and event.get("eventType") in {"keyDown", "rawKeyDown"} and event.get("key") == "Enter"


def writes_value(args: Sequence[str]) -> bool:
    """Whether an agent command can put a value into a field, including within a batch.

    Capture marks the document before every such command runs.
    """
    if not args:
        return False
    if args[0] == "batch":
        try:
            nested = [shlex.split(arg) for arg in args[1:] if arg != "--bail"]
        except ValueError:
            return True
        return any(writes_value(command) for command in nested)
    # wait --fn runs a page script, which can set a field.
    return args[0] not in _NO_VALUE_COMMANDS or args[0] == "wait" and "--fn" in args


def switches_tab(args: Sequence[str]) -> bool:
    """Whether a batch has a tab command: loads in another tab aren't seen from the page the batch started on."""
    if not args or args[0] != "batch":
        return False
    try:
        return any(shlex.split(arg)[:1] == ["tab"] for arg in args[1:])
    except ValueError:
        return True
