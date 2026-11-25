"""Identify person submits and agent commands that can write a page value."""
from __future__ import annotations

import shlex
from typing import Mapping, Sequence

_NO_VALUE_COMMANDS = frozenset({
    "click", "dblclick", "hover", "focus", "scroll", "scrollintoview", "get", "is", "snapshot", "screenshot",
    "wait", "tab", "open", "goto", "navigate", "back", "forward", "reload",
})
_STAYS_ON_DOCUMENT = frozenset({
    "fill", "type", "select", "check", "uncheck", "hover", "focus", "scroll", "scrollintoview", "get", "is",
    "snapshot", "screenshot", "wait",
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


def _may_load(command: Sequence[str]) -> bool:
    # A click on a link, a key that submits or a script can load a new document; these commands never do.
    return not command or command[0] not in _STAYS_ON_DOCUMENT or command[0] == "wait" and "--fn" in command


def types_between_documents(args: Sequence[str]) -> bool:
    """Whether a batch can type into a document it reached by a load and then left by another.

    Marking the documents a batch starts and ends on covers every other batch; this one could leave the
    agent's value in a document in between (one a back navigation can restore), so capture marks every
    document of the session.
    """
    if not args or args[0] != "batch":
        return False
    try:
        nested = [shlex.split(arg) for arg in args[1:] if arg != "--bail"]
    except ValueError:
        return True
    return any(writes_value(command) and any(map(_may_load, nested[:index]))
               and (_may_load(command) or any(map(_may_load, nested[index + 1:])))
               for index, command in enumerate(nested))
