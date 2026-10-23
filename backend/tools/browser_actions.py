"""Single-action browser tools that each wrap one agent-browser CLI command."""

import json
from typing import Any
from urllib.parse import urlparse

from host_access import browser_control
from host_access.browser_domain import registrable_domain
from tools.base import Tool
from tools.browser_runtime import _declare_person_step, _execution_context, _run


_STEP_ACTIONS = frozenset({"goto", "click", "type", "wait_for"})


def _result(**value: object) -> str:
    return json.dumps(value, separators=(",", ":"))


def _allowed(url: str, domains: set[str]) -> bool:
    parsed = urlparse(url)
    return parsed.scheme in {"http", "https"} and registrable_domain(parsed.hostname or "") in domains


def _step_error(steps: object) -> str | None:
    if not isinstance(steps, dict) or set(steps) != {"domains", "steps"}:
        return "steps must contain only domains and steps"
    domains, sequence = steps["domains"], steps["steps"]
    if not isinstance(domains, list) or not domains or not all(isinstance(domain, str) and domain for domain in domains):
        return "domains must be a non-empty list of registrable domains"
    if not isinstance(sequence, list) or not sequence:
        return "steps must be a non-empty list"
    ids: set[str] = set()
    for step in sequence:
        if not isinstance(step, dict) or not {"id", "says", "action"} <= set(step):
            return "each step needs id, says and action"
        identifier, says, action = step["id"], step["says"], step["action"]
        if not isinstance(identifier, str) or not identifier or identifier in ids or not isinstance(says, str) or not says:
            return "each step needs a unique id and one-line says"
        ids.add(identifier)
        if action not in _STEP_ACTIONS:
            return "step action must be goto, click, type or wait_for"
        if action == "goto" and not (step.get("reload") is True or (isinstance(step.get("url"), str) and _allowed(step["url"], set(domains)))):
            return "goto must use an allowed http(s) URL or reload"
        if action == "type" and (not isinstance(step.get("selector"), str) or not isinstance(step.get("value"), str)):
            return "type needs selector and value"
        if action == "click" and not (isinstance(step.get("selector"), str) or isinstance(step.get("script"), str)):
            return "click needs selector or script"
        if action == "wait_for" and (not isinstance(step.get("selector"), str) or not isinstance(step.get("timeout"), int) or step["timeout"] <= 0):
            return "wait_for needs selector and positive timeout"
    return None


class BrowserRunStepsTool(Tool):
    """Run a checked, foreground sequence through the guarded browser session."""

    name = "browser_run_steps"
    description = "Run checked browser setup steps from skill data in the current visible browser. Returns a safe miss or guard result so you can resume at that step."
    parameters = {
        "type": "object",
        "properties": {
            "steps": {"type": "object", "description": "The steps-as-data object from the skill."},
            "start_at": {"type": "string", "description": "Optional step id to resume from."},
            "values": {"type": "object", "description": "Runtime values used by ${name} placeholders."},
        },
        "required": ["steps"],
    }

    async def execute(self, steps: object, start_at: str | None = None, values: object | None = None, **kwargs: Any) -> str:
        thread_id, _project_id = _execution_context(kwargs)
        error = _step_error(steps)
        if error:
            return _result(ok=False, error=error)
        if values is None:
            values = {}
        if not isinstance(values, dict) or not all(isinstance(key, str) and isinstance(value, str) for key, value in values.items()):
            return _result(ok=False, error="values must map names to strings")
        sequence = steps["steps"]
        ids = [step["id"] for step in sequence]
        if start_at is not None and start_at not in ids:
            return _result(ok=False, error="start_at must name a step")
        begin = ids.index(start_at) if start_at else 0
        domains, done = set(steps["domains"]), []
        for step in sequence[begin:]:
            identifier, says = step["id"], step["says"]
            result = await _run(self._command(step, values), thread_id=thread_id, timeout=step.get("timeout", 30))
            if result.startswith("Error:"):
                message = result.removeprefix("Error: ")
                if message in {browser_control.PASSWORD_PAGE, browser_control.PERSON_STEP}:
                    return _result(ok=False, step=identifier, says=says, message=message, start_at=identifier)
                return await self._miss(thread_id, identifier, says, step.get("expected", step.get("selector", "action")))
            saw, url = await self._saw(thread_id, step.get("expected", step.get("selector", "action")))
            if not _allowed(url, domains):
                return _result(ok=False, step=identifier, says=says, saw=saw)
            done.append(identifier)
        return _result(ok=True, steps=done)

    @staticmethod
    def _command(step: dict[str, object], values: dict[str, str]) -> list[str]:
        def fill(value: object) -> str:
            value = str(value)
            for key, replacement in values.items():
                value = value.replace("${" + key + "}", replacement)
            return value
        action = step["action"]
        if action == "goto":
            return ["eval", "location.reload()"] if step.get("reload") is True else ["open", fill(step["url"])]
        if action == "click":
            return ["eval", fill(step["script"])] if "script" in step else ["click", fill(step["selector"])]
        if action == "type":
            return ["fill", fill(step["selector"]), fill(step["value"])]
        return ["wait", fill(step["selector"])]

    async def _saw(self, thread_id: str, expected: object) -> tuple[dict[str, str], str]:
        title = await _run(["get", "title"], thread_id=thread_id)
        url = await _run(["get", "url"], thread_id=thread_id)
        return ({"title": "" if title.startswith("Error:") else title.strip(), "url_path": urlparse(url.strip()).path, "missing": str(expected)}, url.strip())

    async def _miss(self, thread_id: str, identifier: str, says: str, expected: object) -> str:
        saw, _url = await self._saw(thread_id, expected)
        return _result(ok=False, step=identifier, says=says, saw=saw)


class BrowserSnapshotTool(Tool):
    """Get the accessibility tree of the current page."""

    name = "browser_snapshot"
    description = (
        "Get the accessibility tree of the current browser page. "
        "Returns semantic DOM with element refs (e.g. @e42) for interaction. "
        "These actions work alongside the person; password pages are for the person to handle.\n\n"
        "USE THIS to:\n"
        "- See what's on the current page (text, buttons, inputs, links)\n"
        "- Find element refs for browser_click or browser_fill\n"
        "- Verify UI state after interactions\n\n"
        "Prefer this over browser_screenshot for understanding page structure — "
        "it's faster and more AI-friendly."
    )
    parameters = {
        "type": "object",
        "properties": {
            "interactive_only": {
                "type": "boolean",
                "description": "Only show interactive elements (buttons, inputs, links). Default: false (show all)."
            },
        },
        "required": [],
    }

    async def execute(self, interactive_only: bool = False, **kwargs: Any) -> str:
        thread_id, _project_id = _execution_context(kwargs)
        args = ["snapshot"]
        if interactive_only:
            args.append("-i")
        return await _run(
            args,
            thread_id=thread_id,
            timeout=15,
        )



class BrowserClickTool(Tool):
    """Click an element in the browser."""

    name = "browser_click"
    description = (
        "Click an element in the browser by ref (from snapshot) or CSS selector. "
        "These actions work alongside the person. Never click what would show a password, key or code "
        "(such as creating or revealing a key) or accept a consent screen's request for account access: "
        "post_update the person to click it, then watch only the page's URL and title. Only a skill "
        "that saves the password, key or code unseen may have you click to show it yourself.\n\n"
        "USE THIS to:\n"
        "- Click buttons, links, checkboxes\n"
        "- Navigate between pages\n"
        "- Trigger UI actions\n\n"
        "Get element refs from browser_snapshot (e.g. @e42)."
    )
    parameters = {
        "type": "object",
        "properties": {
            "selector": {
                "type": "string",
                "description": "Element ref from snapshot (e.g. '@e42') or CSS selector"
            },
        },
        "required": ["selector"],
    }

    async def execute(self, selector: str, **kwargs: Any) -> str:
        thread_id, _project_id = _execution_context(kwargs)
        return await _run(
            ["click", selector],
            thread_id=thread_id,
        )



class BrowserFillTool(Tool):
    """Fill a text input in the browser."""

    name = "browser_fill"
    description = (
        "Clear and fill a text input in the browser. "
        "These actions work alongside the person; password pages are for the person to handle.\n\n"
        "USE THIS to:\n"
        "- Fill form fields, search boxes, text areas\n"
        "- Enter credentials or test data\n\n"
        "Get element refs from browser_snapshot (e.g. @e5)."
    )
    parameters = {
        "type": "object",
        "properties": {
            "selector": {
                "type": "string",
                "description": "Element ref from snapshot (e.g. '@e5') or CSS selector"
            },
            "text": {
                "type": "string",
                "description": "Text to fill into the element"
            },
        },
        "required": ["selector", "text"],
    }

    async def execute(self, selector: str, text: str, **kwargs: Any) -> str:
        thread_id, _project_id = _execution_context(kwargs)
        return await _run(
            ["fill", selector, text],
            thread_id=thread_id,
        )



class BrowserEvalTool(Tool):
    """Run JavaScript in the browser and return the result."""

    name = "browser_eval"
    description = (
        "Run JavaScript in the current browser page and return the result. "
        "These actions work alongside the person; password pages are for the person to handle.\n\n"
        "USE THIS to:\n"
        "- Extract data from the page (e.g. document.title, localStorage)\n"
        "- Check computed state or values\n"
        "- Perform actions not possible through the accessibility tree\n\n"
        "Note: Do NOT use 'return' — the last expression is the result."
    )
    parameters = {
        "type": "object",
        "properties": {
            "script": {
                "type": "string",
                "description": "JavaScript to evaluate. Last expression is the return value. No 'return' keyword."
            },
        },
        "required": ["script"],
    }

    async def execute(self, script: str, **kwargs: Any) -> str:
        thread_id, _project_id = _execution_context(kwargs)
        return await _run(
            ["eval", script],
            thread_id=thread_id,
        )



class BrowserPersonStepTool(Tool):
    """Let the person complete a visible browser page and wake the agent after navigation."""

    name = "browser_person_step"
    description = (
        "Give the current visible browser page to the person to complete, then end your turn. "
        "CORSIA puts the step in their attention list and wakes you when the page changes. "
        "Use this for sign-in, account choice, verification, passkeys, or phone prompts; never ask for or "
        "read a password or code."
    )
    parameters = {
        "type": "object",
        "properties": {
            "instruction": {
                "type": "string",
                "description": "The short action the person should complete, such as 'Sign in to Google'.",
            },
        },
        "required": ["instruction"],
    }

    async def execute(self, instruction: str, **kwargs: Any) -> str:
        thread_id, _project_id = _execution_context(kwargs)
        return await _declare_person_step(thread_id, instruction)


class BrowserWaitTool(Tool):
    """Wait for an element to appear or a fixed time."""

    name = "browser_wait"
    description = (
        "Wait for an element to appear on the page, or wait a fixed number of milliseconds. "
        "These actions work alongside the person; password pages are for the person to handle.\n\n"
        "USE THIS to:\n"
        "- Wait for async content to load after an action\n"
        "- Pause between interactions\n"
        "- Wait for navigation to complete\n\n"
        "Pass a CSS selector or @ref to wait for an element, or a number (ms) to wait a fixed time."
    )
    parameters = {
        "type": "object",
        "properties": {
            "target": {
                "type": "string",
                "description": "Element selector/ref to wait for (e.g. '@e5', '.my-class'), OR milliseconds as string (e.g. '1000')"
            },
        },
        "required": ["target"],
    }

    async def execute(self, target: str, **kwargs: Any) -> str:
        thread_id, _project_id = _execution_context(kwargs)
        return await _run(
            ["wait", target],
            thread_id=thread_id,
            timeout=30,
        )

