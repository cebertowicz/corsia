"""Guarded browser setup steps from effective skills."""

import asyncio
import json
import re
import time
from typing import Any
from urllib.parse import urlparse

from loguru import logger

from core.skills_registry import resolve_effective_skills
from host_access import browser_cli, browser_control
from host_access.browser_domain import registrable_domain
from infra import config
from infra.database import get_db
from infra.system_package import SYSTEM_VERSION
from library.installed import read_origin
from tools.base import Tool
from tools.browser_runtime import BrowserOwnerError, _execution_context, _run_owned

_SAFE_NAME = re.compile(r"[a-z0-9-]+")
_RETRY_PAUSE_S = 1.0  # between selector waits, so an instant error is not a tight loop of browser launches
_GUARD_REFUSALS = (browser_control.PASSWORD_PAGE, browser_control.PERSON_STEP,
                   browser_control.PASSWORD_DECLINED, browser_control.PERSON_STEP_DECLINED)


def _result(**value: object) -> str:
    return json.dumps(value, separators=(",", ":"))


async def _call(args: list[str], *, thread_id: str) -> str:
    try:
        return await _run_owned(args, thread_id=thread_id)
    except BrowserOwnerError as exc:
        if exc.text.removeprefix("Error: ").startswith(_GUARD_REFUSALS):
            return exc.text
        raise


def _allowed(url: str, domains: set[str]) -> bool:
    parsed = urlparse(url)
    return parsed.scheme in {"http", "https"} and registrable_domain(parsed.hostname or "") in domains


def _load(steps: str, thread_id: str, project_id: str | None) -> tuple[dict[str, Any] | None, str | None]:
    parts = steps.split("/")
    if len(parts) != 2 or not all(_SAFE_NAME.fullmatch(part) for part in parts):
        return None, "unknown steps file"
    db = get_db()
    try:
        skills = resolve_effective_skills(thread_id=thread_id, project_id=project_id, workspace=config.WORKSPACE, db=db)
    finally:
        db.close()
    skill = next((row for row in skills.included if row.name == parts[0]), None)
    if skill is None:
        return None, "unknown steps file"
    try:
        folder = skill.path.parent.resolve()
        steps_dir = (folder / "steps").resolve()
        path = (steps_dir / f"{parts[1]}.json").resolve()
        if steps_dir != folder / "steps" or not path.is_relative_to(steps_dir):
            return None, "unknown steps file"
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, RuntimeError, json.JSONDecodeError):
        return None, "unknown steps file"
    origin = read_origin(folder) or {}
    version = SYSTEM_VERSION if skill.scope == "system" else origin.get("version") or origin.get("sha256") or skill.metadata.get("version") or "unversioned"
    logger.info("browser setup steps source={} skill={} version={}", "system" if skill.scope == "system" else "installed", skill.name, version)
    return data, _validate(data)


def _validate(data: object) -> str | None:
    if not isinstance(data, dict) or not {"domains", "steps"} <= set(data) or set(data) - {"domains", "steps", "note"}:
        return "steps file must contain domains and steps"
    domains, sequence = data["domains"], data["steps"]
    if not isinstance(domains, list) or not domains or not all(isinstance(item, str) and item for item in domains):
        return "domains must be a non-empty list"
    if not isinstance(sequence, list) or not sequence:
        return "steps must be a non-empty list"
    identifiers: set[str] = set()
    allowed = {"goto": {"url", "reload"}, "click": {"selector"}, "type": {"selector", "value"},
               "wait_for": {"selector", "timeout", "ms"}}
    for step in sequence:
        if not isinstance(step, dict) or not {"id", "says", "action"} <= set(step):
            return "each step needs id, says and action"
        identifier, says, action = step["id"], step["says"], step["action"]
        if not isinstance(identifier, str) or not identifier or identifier in identifiers or not isinstance(says, str) or not says:
            return "each step needs a unique id and says"
        identifiers.add(identifier)
        if not isinstance(action, str) or action not in allowed:
            return "step action must be goto, click, type or wait_for"
        if set(step) - {"id", "says", "action"} - allowed[action]:
            return "unknown step key"
        if action == "goto" and not (step.get("reload") is True or isinstance(step.get("url"), str) and _allowed(step["url"], set(domains))):
            return "goto must use an allowed http(s) URL or reload"
        if action == "click" and not isinstance(step.get("selector"), str):
            return "click needs a selector"
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
    description = "Run a browser setup file from a System or installed skill in the visible browser. Returns a safe miss or guard result so you can resume at that step."
    parameters = {
        "type": "object",
        "properties": {
            "steps": {"type": "string", "description": "System or installed skill step file, for example google-setup/console-project."},
            "start_at": {"type": "string", "description": "Optional step id to resume from."},
        },
        "required": ["steps"],
    }

    async def execute(self, steps: str, start_at: str | None = None, **kwargs: Any) -> str:
        try:
            return await self._execute(steps, start_at, **kwargs)
        except BrowserOwnerError as exc:
            return _result(ok=False, error=exc.text.removeprefix("Error: "))

    async def _execute(self, steps: str, start_at: str | None = None, **kwargs: Any) -> str:
        thread_id, project_id = _execution_context(kwargs)
        data, error = await asyncio.to_thread(_load, steps, thread_id, project_id) if isinstance(steps, str) else (None, "unknown steps file")
        if error or data is None:
            return _result(ok=False, error=error or "unknown steps file")
        sequence = data["steps"]
        identifiers = [step["id"] for step in sequence]
        if start_at is not None and start_at not in identifiers:
            return _result(ok=False, error="start_at must name a step")
        done, domains = [], set(data["domains"])
        begin = identifiers.index(start_at) if start_at else 0
        for index in range(begin, len(sequence)):
            step = sequence[index]
            identifier, says = step["id"], step["says"]
            expected = step.get("selector", "action")
            if step["action"] in {"click", "type"}:
                saw, url, refusal = await _saw(thread_id, expected)
                if refusal:
                    return _guard_result(identifier, says, refusal)
                if not _allowed(url, domains):
                    return _result(ok=False, step=identifier, says=says, saw=saw)
            deadline = time.monotonic() + step.get("timeout", 0)
            while True:
                result = await _call(_command(step), thread_id=thread_id)
                message = result.removeprefix("Error: ")
                if (step["action"] != "wait_for" or "selector" not in step or
                    message.startswith(_GUARD_REFUSALS) or
                    not (result.startswith("Error:") or result == browser_cli.WAIT_TIMED_OUT) or
                    time.monotonic() >= deadline):
                    break
                await asyncio.sleep(_RETRY_PAUSE_S)
            if result == browser_cli.WAIT_TIMED_OUT:
                result = "Error: wait timed out"
            if result.startswith("Error:"):
                message = result.removeprefix("Error: ")
                if message.startswith(_GUARD_REFUSALS):
                    return _guard_result(identifier, says, message)
                return await _miss(thread_id, identifier, says, expected)
            saw, url, refusal = await _saw(thread_id, expected)
            if refusal:
                done.append(identifier)
                if index + 1 == len(sequence):
                    return _result(ok=True, steps=done, message=refusal)
                next_step = sequence[index + 1]
                return _guard_result(next_step["id"], next_step["says"], refusal)
            if not _allowed(url, domains):
                return _result(ok=False, step=identifier, says=says, saw=saw)
            done.append(identifier)
        return _result(ok=True, steps=done)


def _command(step: dict[str, Any]) -> list[str]:
    action = step["action"]
    if action == "goto":
        return ["reload"] if step.get("reload") else ["open", step["url"]]
    if action == "click":
        return ["click", step["selector"]]
    if action == "type":
        return ["fill", step["selector"], step["value"]]
    return ["wait", str(step["ms"])] if "ms" in step else ["wait", step["selector"]]


def _guard_result(identifier: str, says: str, message: str) -> str:
    return _result(ok=False, step=identifier, says=says, message=message, start_at=identifier)


async def _saw(thread_id: str, expected: str) -> tuple[dict[str, str], str, str | None]:
    title = await _call(["get", "title"], thread_id=thread_id)
    message = title.removeprefix("Error: ") if title.startswith("Error:") else ""
    if message.startswith(_GUARD_REFUSALS):
        return {"title": "", "url_path": "", "missing": expected}, "", message
    url = await _call(["get", "url"], thread_id=thread_id)
    message = url.removeprefix("Error: ") if url.startswith("Error:") else ""
    if message.startswith(_GUARD_REFUSALS):
        return {"title": "", "url_path": "", "missing": expected}, "", message
    safe_title = "" if title.startswith("Error:") else title.strip()
    safe_url = "" if url.startswith("Error:") else url.strip()
    return {"title": safe_title, "url_path": urlparse(safe_url).path, "missing": expected}, safe_url, None


async def _miss(thread_id: str, identifier: str, says: str, expected: str) -> str:
    saw, _url, refusal = await _saw(thread_id, expected)
    if refusal:
        return _guard_result(identifier, says, refusal)
    return _result(ok=False, step=identifier, says=says, saw=saw)
