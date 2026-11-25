"""One browser tool for keys, dropdown choice, scrolling, hover and history; each action is one fixed command shape."""

from typing import Any

from host_access.browser_agent_verbs import DIRECTIONS, KEY_REFUSAL, KEYS, MAX_SCROLL
from tools.base import Tool
from tools.browser_runtime import _execution_context, _run

ACTIONS = ("press", "select", "scroll", "hover", "back", "forward")
NEEDS = {"press": "key", "select": "selector and value", "hover": "selector"}


class BrowserInputTool(Tool):
    """Press a key, choose a dropdown option, scroll, hover, or go back or forward."""

    name = "browser_input"
    description = (
        "Do one input on the current browser page that browser_click and browser_fill can't. "
        "Password pages are for the person to handle. While the person has the browser open, press, hover and "
        "scrolling by direction are refused, since they would act as the person's own input; select, scroll with "
        "a selector, back and forward still work.\n\n"
        "Actions:\n"
        "- press: one key for the focused element, from the key list only. Enter submits a search or form, "
        "Tab and Shift+Tab move focus, arrows drive a date picker or list, Escape closes a popup. "
        "Type text with browser_fill.\n"
        "- select: choose an option of a native <select> (selector, value or label). Use browser_click for a custom dropdown.\n"
        "- scroll: scroll the page by direction (and amount in pixels), or pass selector to bring an element into view; "
        "use it to load a list that fills in as you scroll.\n"
        "- hover: move the pointer over selector, to open a menu or tooltip.\n"
        "- back, forward: one page in history. The page may load again from the site, so check its form values.\n\n"
        "Get element refs from browser_snapshot (e.g. @e5), and snapshot again to see the result."
    )
    parameters = {
        "type": "object",
        "properties": {
            "action": {"type": "string", "enum": list(ACTIONS), "description": "What to do"},
            "key": {"type": "string", "enum": list(KEYS), "description": "For press: the key"},
            "selector": {"type": "string", "description": "For select, hover, scroll: element ref from snapshot (e.g. '@e5') or CSS selector"},
            "value": {"type": "string", "description": "For select: the option's value or visible label"},
            "direction": {"type": "string", "enum": list(DIRECTIONS), "description": "For scroll without selector: default down"},
            "amount": {"type": "integer", "minimum": 1, "maximum": MAX_SCROLL, "description": "For scroll with direction: pixels, default 300"},
        },
        "required": ["action"],
    }

    async def execute(self, action: str, key: str | None = None, selector: str | None = None,
                      value: str | None = None, direction: str = "down", amount: int | None = None,
                      **kwargs: Any) -> str:
        thread_id, _project_id = _execution_context(kwargs)
        args = _command(action, key, selector, value, direction, amount)
        if isinstance(args, str):
            return f"Error: {args}"
        return await _run(args, thread_id=thread_id)


def _command(action: str, key: str | None, selector: str | None, value: str | None,
             direction: str, amount: int | None) -> list[str] | str:
    """The one fixed command for the action, or what to fix."""
    if action not in ACTIONS:
        return f"action is one of {', '.join(ACTIONS)}."
    if action == "press":
        if key is None:
            return "press needs a key."
        return ["press", key] if key in KEYS else KEY_REFUSAL
    if action in {"back", "forward"}:
        return [action]
    if action == "scroll":
        if selector is not None:
            return ["scrollintoview", selector]
        if direction not in DIRECTIONS:
            return f"direction is one of {', '.join(DIRECTIONS)}."
        return ["scroll", direction, *([str(amount)] if amount is not None else [])]
    if not selector or action == "select" and value is None:
        return f"{action} needs {NEEDS[action]}."
    return ["select", selector, value or ""] if action == "select" else ["hover", selector]
