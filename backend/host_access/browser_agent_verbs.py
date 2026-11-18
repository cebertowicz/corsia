"""Agent keys, dropdown choice, scrolling, hover and history on a browser session with a saved sign-in.

Each verb is admitted as one fixed command shape (``admitted``), and its result carries no page content
(``ACKNOWLEDGED``).
"""
from __future__ import annotations

import re
from typing import Sequence

VERBS = frozenset({"press", "select", "scroll", "scrollintoview", "hover", "back", "forward"})
# agent-browser's own names for the fixed key list; anything else, modifiers and text included, is refused.
KEYS = ("Enter", "Tab", "Shift+Tab", "Escape", "ArrowUp", "ArrowDown", "ArrowLeft", "ArrowRight",
        "Home", "End", "PageUp", "PageDown", "Space", "Backspace", "Delete")
DIRECTIONS = ("up", "down", "left", "right")
MAX_SCROLL = 5000
KEY_REFUSAL = ("press takes one of these keys only: Enter, Tab, Shift+Tab, Escape, ArrowUp, ArrowDown, "
               "ArrowLeft, ArrowRight, Home, End, PageUp, PageDown, Space, Backspace, Delete. "
               "Type text with browser_fill.")
ACKNOWLEDGED = "Done."
# What agent-browser 0.31.2 says for a target that isn't there: it names nothing from the page.
NOT_FOUND = ("Error: agent-browser command failed: Element not found. "
             "Verify the selector is correct and the element exists in the DOM.")
# Its cover message names the covering element (a tag), so a fixed text stands in for it.
COVERED = ("Error: agent-browser command failed: that element is covered by another element at its click point. "
           "Dismiss or interact with the covering element first (it is often a dialog, banner, or sticky header).")
UNUSABLE = "Error: agent-browser command failed: that element couldn't be used."
TARGETED = frozenset({"select", "hover", "scrollintoview"})
# Verbs agent-browser sends as real keyboard, pointer or wheel input. While the person has the pane open they
# would land in the person's focus or move their pointer, so they are refused (AR1: a write runs inside the
# page or not at all). select runs as a page-context write and scrollintoview moves no input, so both stay.
PERSON_INPUT = frozenset({"press", "hover", "scroll"})
PANE_INPUT_REFUSAL = (
    "The person has this browser open, so keys, hover and scrolling by direction would act as their own input, "
    "and nothing was sent. Click the control instead (for Enter, the form's submit button), scroll with a "
    "selector to bring an element into view, or ask the person in chat.")

_REF = re.compile(r"(?:@|ref=)?e\d+")
# Other selector engines: a prefix such as text=, xpath= or js= (the parser allows space before '='), an XPath,
# a quoted text selector, a chained locator, or a script URL.
_ENGINE = re.compile(r"[A-Za-z][A-Za-z0-9_:-]*\s*=")
_CONTROL = re.compile(r"[\x00-\x1f\x7f]")


def plain_target(selector: str) -> bool:
    """A snapshot ref or a plain CSS selector; never an engine prefix, XPath, chain or script."""
    if _REF.fullmatch(selector):
        return True
    lowered = selector.lower()
    # The selector parser trims each part, so a check on the untrimmed text could miss a prefix.
    return not (not selector or selector != selector.strip() or selector.startswith(("-", "/", "(", "..", '"', "'")) or
                _ENGINE.match(selector) or ">>" in selector or _CONTROL.search(selector) or
                "javascript:" in lowered or "eval(" in lowered)


def admitted(args: Sequence[str]) -> bool:
    """Whether the arguments are exactly one of the fixed shapes of these verbs."""
    command, rest = args[0], args[1:]
    if command == "press":
        return len(rest) == 1 and rest[0] in KEYS
    if command in {"back", "forward"}:
        return not rest
    if command in {"hover", "scrollintoview"}:
        return len(rest) == 1 and plain_target(rest[0])
    if command == "select":
        return len(rest) == 2 and plain_target(rest[0]) and not rest[1].startswith("-")
    if command == "scroll":
        return (1 <= len(rest) <= 2 and rest[0] in DIRECTIONS and
                (len(rest) == 1 or rest[1].isascii() and rest[1].isdigit() and 0 < int(rest[1]) <= MAX_SCROLL))
    return False


def refusal(args: Sequence[str]) -> str | None:
    """The reason a press outside the key list is refused, so the agent learns the list."""
    return KEY_REFUSAL if args[0] == "press" else None


def model_error(args: Sequence[str], text: str) -> str:
    """A target verb's error with nothing from the page in it; only the known not-found text passes through."""
    if args[0] not in TARGETED or not text.startswith("Error:") or text == NOT_FOUND:
        return text
    return COVERED if " is covered by " in text else UNUSABLE
