"""Guarded, server-owned browser setup steps."""

import json
import re
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

from host_access import browser_control
from host_access.browser_domain import registrable_domain
from tools.base import Tool
from tools.browser_runtime import _execution_context, _run

_ACTIONS = frozenset({"goto", "click", "type", "wait_for"})
_PLACEHOLDER = re.compile(r"\$\{[^}]+}")
_SAFE_NAME = re.compile(r"[a-z0-9-]+")
_SKILLS = Path(__file__).resolve().parents[1] / "prompts/system/v1/skills"


def _result(**value: object) -> str:
    return json.dumps(value, separators=(",", ":"))


def _allowed(url: str, domains: set[str]) -> bool:
    parsed = urlparse(url)
    return parsed.scheme in {"http", "https"} and registrable_domain(parsed.hostname or "") in domains


def _load(steps: str) -> tuple[dict[str, Any] | None, str | None]:
    parts = steps.split("/")
    if len(parts) != 2 or not all(_SAFE_NAME.fullmatch(part) for part in parts):
        return None, "unknown steps file"
    path = _SKILLS / parts[0] / "steps" / f"{parts[1]}.json"
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (FileNotFoundError, json.JSONDecodeError):
        return None, "unknown steps file"
    return data, _validate(data)


def _validate(data: object) -> str | None:
    if not isinstance(data, dict) or not {"domains", "steps"} <= set(data) or set(data) - {"domains", "steps", "note"}:
        return "steps file must contain domains and steps"
    domains, sequence = data["domains"], data["steps"]
    if not isinstance(domains, list) or not domains or not all(isinstance(item, str) and item for item in domains):
        return "domains must be a non-empty list"
    if _PLACEHOLDER.search(str(data.get("note", ""))) or any(_PLACEHOLDER.search(domain) for domain in domains):
        return "placeholders are allowed only in type values"
    if not isinstance(sequence, list) or not sequence:
        return "steps must be a non-empty list"
    identifiers: set[str] = set()
    for step in sequence:
        if not isinstance(step, dict) or not {"id", "says", "action"} <= set(step):
            return "each step needs id, says and action"
        identifier, says, action = step["id"], step["says"], step["action"]
        if not isinstance(identifier, str) or not identifier or identifier in identifiers or not isinstance(says, str) or not says:
            return "each step needs a unique id and says"
        identifiers.add(identifier)
        if action not in _ACTIONS:
            return "step action must be goto, click, type or wait_for"
        if any(_PLACEHOLDER.search(value) for key, value in step.items() if isinstance(value, str) and (action != "type" or key != "value")):
            return "placeholders are allowed only in type values"
        if action == "goto" and not (step.get("reload") is True or isinstance(step.get("url"), str) and _allowed(step["url"], set(domains))):
            return "goto must use an allowed http(s) URL or reload"
        if action == "click" and not (isinstance(step.get("selector"), str) ^ isinstance(step.get("script"), str)):
            return "click needs exactly one selector or script"
        if action == "type" and (not isinstance(step.get("selector"), str) or not isinstance(step.get("value"), str)):
            return "type needs selector and value"
        if action == "wait_for":
            selector, timeout, milliseconds = step.get("selector"), step.get("timeout"), step.get("ms")
            if not ((isinstance(selector, str) and isinstance(timeout, int) and timeout > 0 and milliseconds is None) or
                    (selector is None and timeout is None and isinstance(milliseconds, int) and milliseconds > 0)):
                return "wait_for needs selector and positive timeout, or positive ms"
    return None


class BrowserRunStepsTool(Tool):
    """Run checked foreground setup steps through the guarded browser session."""

    name = "browser_run_steps"
    description = "Run a named server-owned browser setup file in the visible browser. Returns a safe miss or guard result so you can resume at that step."
    parameters = {
        "type": "object",
        "properties": {
            "steps": {"type": "string", "description": "Server-owned skill step file, for example google-setup/console-project."},
            "start_at": {"type": "string", "description": "Optional step id to resume from."},
            "values": {"type": "object", "description": "Runtime strings for placeholders in type values."},
        },
        "required": ["steps"],
    }

    async def execute(self, steps: str, start_at: str | None = None, values: object | None = None, **kwargs: Any) -> str:
        data, error = _load(steps) if isinstance(steps, str) else (None, "unknown steps file")
        if error or data is None:
            return _result(ok=False, error=error or "unknown steps file")
        if values is None:
            values = {}
        if not isinstance(values, dict) or not all(isinstance(key, str) and isinstance(value, str) for key, value in values.items()):
            return _result(ok=False, error="values must map names to strings")
        sequence = data["steps"]
        identifiers = [step["id"] for step in sequence]
        if start_at is not None and start_at not in identifiers:
            return _result(ok=False, error="start_at must name a step")
        thread_id, _project_id = _execution_context(kwargs)
        done, domains = [], set(data["domains"])
        begin = identifiers.index(start_at) if start_at else 0
        for step in sequence[begin:]:
            identifier, says = step["id"], step["says"]
            result = await _run(_command(step, values), thread_id=thread_id, timeout=_timeout(step))
            if result.startswith("Error:"):
                message = result.removeprefix("Error: ")
                if message.startswith(browser_control.PASSWORD_PAGE) or message.startswith(browser_control.PERSON_STEP):
                    return _guard_result(identifier, says, message)
                return await _miss(thread_id, identifier, says, _expected(step))
            saw, url, refusal = await _saw(thread_id, _expected(step))
            if refusal:
                return _guard_result(identifier, says, refusal)
            if not _allowed(url, domains):
                return _result(ok=False, step=identifier, says=says, saw=saw)
            done.append(identifier)
        return _result(ok=True, steps=done)


def _command(step: dict[str, Any], values: dict[str, str]) -> list[str]:
    action = step["action"]
    if action == "goto":
        return ["eval", "location.reload()"] if step.get("reload") else ["open", step["url"]]
    if action == "click":
        return ["eval", step["script"]] if "script" in step else ["click", step["selector"]]
    if action == "type":
        return ["fill", step["selector"], _substitute(step["value"], values)]
    return ["wait", str(step["ms"])] if "ms" in step else ["wait", step["selector"]]


def _substitute(value: str, values: dict[str, str]) -> str:
    return _PLACEHOLDER.sub(lambda match: values.get(match.group()[2:-1], match.group()), value)


def _timeout(step: dict[str, Any]) -> int:
    return step.get("timeout", max(1, (step.get("ms", 0) + 999) // 1000))


def _expected(step: dict[str, Any]) -> str:
    return str(step.get("expected", step.get("selector", "action")))


def _guard_result(identifier: str, says: str, message: str) -> str:
    return _result(ok=False, step=identifier, says=says, message=message, start_at=identifier)


async def _saw(thread_id: str, expected: str) -> tuple[dict[str, str], str, str | None]:
    title = await _run(["get", "title"], thread_id=thread_id)
    message = title.removeprefix("Error: ") if title.startswith("Error:") else ""
    if message.startswith(browser_control.PASSWORD_PAGE) or message.startswith(browser_control.PERSON_STEP):
        return {"title": "", "url_path": "", "missing": expected}, "", message
    url = await _run(["get", "url"], thread_id=thread_id)
    message = url.removeprefix("Error: ") if url.startswith("Error:") else ""
    if message.startswith(browser_control.PASSWORD_PAGE) or message.startswith(browser_control.PERSON_STEP):
        return {"title": "", "url_path": "", "missing": expected}, "", message
    safe_title = "" if title.startswith("Error:") else title.strip()
    safe_url = "" if url.startswith("Error:") else url.strip()
    return {"title": safe_title, "url_path": urlparse(safe_url).path, "missing": expected}, safe_url, None


async def _miss(thread_id: str, identifier: str, says: str, expected: str) -> str:
    saw, _url, refusal = await _saw(thread_id, expected)
    return _guard_result(identifier, says, refusal) if refusal else _result(ok=False, step=identifier, says=says, saw=saw)
