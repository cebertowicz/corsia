"""Execution, cleanup and capability adapters for the browser tool family."""
from __future__ import annotations

import shutil
from typing import Any

from host_access import browser_cli, browser_control
from host_access.browser_identity import BrowserIdentityError
from host_access.browser_sessions import (
    BrowserSessionCleanupError,
    BrowserSessionError,
    BrowserSetupError,
)
from host_access.processes import PROCESS_OWNER
from tools.base import Tool

_AGENT_BROWSER_BIN = "agent-browser"
_DEFAULT_TIMEOUT = 30


def _native_problem() -> str | None:
    """Apply the owner's own executable admission, so capability and use agree."""
    try:
        browser_cli.resolve_native(_AGENT_BROWSER_BIN)
    except BrowserIdentityError as exc:
        return str(exc)
    return None


def _check_installed() -> str | None:
    """Return None if agent-browser is admissible, or a setup error string."""
    problem = _native_problem()
    return BrowserSetupError(problem).tool_message if problem else None


def _execution_context(kwargs: dict[str, Any]) -> tuple[str, str | None]:
    thread_id = kwargs.pop("_thread_id", None)
    project_id = kwargs.pop("_thread_project_id", None)
    kwargs.pop("_host_access_turn_identity", None)
    if not isinstance(thread_id, str) or not thread_id:
        raise PermissionError("browser tool requires exact thread identity")
    return thread_id, project_id


async def _run(args: list[str], *, thread_id: str, lease_id: str | None = None,
               timeout: int = _DEFAULT_TIMEOUT, show_window: bool | None = None) -> str:
    """Run through the one backend-owned opaque browser session owner."""
    err = _check_installed()
    if err:
        return err
    try:
        return await PROCESS_OWNER.run_browser(
            thread_id, args, lease_id=lease_id, timeout=timeout, show_window=show_window,
        )
    except (BrowserSessionCleanupError, BrowserSetupError) as exc:
        return exc.tool_message
    except BrowserSessionError as exc:
        return f"Error: {exc}"
    except Exception:
        return "Error: browser command failed. Stop automatic retries and inform the user if browser work remains blocked."


class BrowserCloseTool(Tool):
    """Close the browser session."""

    name = "browser_close"
    description = (
        "Close the current browser session.\n\n"
        "USE THIS when:\n"
        "- Done with browser testing\n"
        "- Need to start fresh with a new session\n"
        "- Cleaning up after a test run"
    )
    parameters = {
        "type": "object",
        "properties": {},
        "required": [],
    }

    async def execute(self, **kwargs: Any) -> str:
        thread_id, _project_id = _execution_context(kwargs)
        try:
            return await PROCESS_OWNER.close_browser(thread_id)
        except BrowserSessionCleanupError as exc:
            return exc.tool_message
        except BrowserSessionError as exc:
            return f"Error: {exc}"
        except Exception:
            return "Error: browser cleanup could not be verified. Stop retrying browser tools and inform the user."


def browser_available() -> dict:
    """Return browser automation capability status.

    The version is checked on first use; this reports what resolve_native admits.
    """
    problem = _native_problem()
    return {
        "available": problem is None,
        "binary": (browser_cli.bundled_executable() or shutil.which(_AGENT_BROWSER_BIN)) if problem is None else None,
        "tool": _AGENT_BROWSER_BIN,
        "install_hint": problem,
    }
