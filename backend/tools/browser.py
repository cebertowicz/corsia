"""Browser automation tools wrapping the agent-browser CLI."""

from __future__ import annotations

import asyncio
import json
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit


from infra.default_port import DEFAULT_BACKEND_PORT
from tools.base import Tool
from tools.browser_runtime import (
    BrowserCloseTool as BrowserCloseTool,
    browser_available as browser_available,
    _execution_context,
    _run,
)
from tools.filesystem_effects import copy_bound_file
from tools.filesystem_gate import _resolve_path
from tools.private_runtime_files import BROWSER_STAGING_DIR, private_mkdtemp


class BrowserOpenTool(Tool):
    """Navigate the browser to a URL."""

    def __init__(self, workspace: Path | None = None) -> None:
        self._workspace = workspace

    name = "browser_open"
    description = (
        "Navigate to a URL in the browser. Opens a new browser session if none is active. "
        "For a new session, show_window=true opens a visible host window. For a running headless session, "
        "show_window=true shares it in CORSIA without relaunching or changing its launch mode. "
        "browser_person_step shares it automatically when you need their help.\n\n"
        "USE THIS to:\n"
        "- Open a web page for inspection or testing\n"
        f"- Navigate to CORSIA UI (http://localhost:{DEFAULT_BACKEND_PORT}) for UX review\n\n"
        "After opening, call browser_snapshot to see the page content."
    )
    parameters = {
        "type": "object",
        "properties": {
            "url": {
                "type": "string",
                "description": f"URL to navigate to (e.g. 'http://localhost:{DEFAULT_BACKEND_PORT}')",
            },
            "wait_for_load": {
                "type": "boolean",
                "description": "Wait for network idle after navigation (default: true). Set false for fast pages.",
            },
            "show_window": {
                "type": "boolean",
                "description": "Open a visible host window for a new session, or share a running headless session without relaunching; default false.",
            },
        },
        "required": ["url"],
    }

    async def execute(self, url: str, wait_for_load: bool = True,
                      show_window: bool = False, **kwargs: Any) -> str:
        thread_id, _ = _execution_context(kwargs)
        from host_access.browser_navigation import normalize_url
        from host_access.browser_session_state import BrowserSessionError
        try:
            effective_url = normalize_url(url)
        except BrowserSessionError as exc:
            return f"Error: {exc}"

        # agent-browser's open-URL command waits for navigation inside the
        # command itself. Long-lived applications can therefore make it time out
        # before this wrapper gets a chance to honor wait_for_load=False.
        # Launch a neutral page first, then schedule navigation through eval so
        # callers control whether to wait for the destination.
        from host_access.processes import PROCESS_OWNER
        from host_access import browser_cli
        sessions = PROCESS_OWNER.browser_sessions()
        existing = await sessions.has_session(thread_id)
        share_existing = existing and show_window and not await sessions.is_shared(thread_id)
        result = await _run(
            ["get", "url"] if existing else ["open", "about:blank"],
            thread_id=thread_id,
            show_window=show_window,
            notice=False,  # Deliver a one-time session notice with the navigation result returned below.
        )
        if result.startswith("Error"):
            return result
        if share_existing and _same_page_url(browser_cli.json_object(result).get("url"), effective_url):
            return f"Shared the current page in the person's CORSIA pane: {effective_url}"

        navigation_result = await _run(
            [
                "eval",
                f"location.href = {json.dumps(effective_url)}; 'navigating'",
            ],
            thread_id=thread_id,
            navigation_url=effective_url,
        )
        if navigation_result.startswith("Error"):
            return navigation_result

        if wait_for_load:
            wait_result = await _run(
                ["wait", "--load", "networkidle"],
                thread_id=thread_id,
                timeout=15,
            )
            return f"{navigation_result}\n{wait_result}".strip()
        return navigation_result


def _same_page_url(reported: object, wanted: str) -> bool:
    """Whether Chrome's reported address is the requested one, as Chrome canonicalizes it.

    Chrome lowercases the scheme and host and reports an empty path as "/", so
    "https://Example.com" is already showing when it reports "https://example.com/".
    """
    if not isinstance(reported, str):
        return False

    def canonical(url: str) -> tuple[str, str, str, str, str]:
        parts = urlsplit(url)
        return (parts.scheme.lower(), parts.netloc.lower(), parts.path or "/", parts.query, parts.fragment)

    return canonical(reported) == canonical(wanted)


class BrowserScreenshotTool(Tool):
    """Take a screenshot of the current browser page."""

    def __init__(self, workspace: Path | None = None) -> None:
        self._workspace = workspace

    name = "browser_screenshot"
    description = (
        "Take a screenshot of the current browser page and save to a file.\n\n"
        "USE THIS to:\n"
        "- Capture visual state for review\n"
        "- Document UI bugs or regressions\n"
        "- Get a visual snapshot when accessibility tree isn't enough\n\n"
        "Prefer browser_snapshot for understanding structure — use this for visual verification."
    )
    parameters = {
        "type": "object",
        "properties": {
            "path": {
                "type": "string",
                "description": "File path to save the screenshot. Defaults to the requesting thread's working/screenshots directory.",
            },
            "full_page": {
                "type": "boolean",
                "description": "Capture full scrollable page (default: false, captures viewport only)",
            },
        },
        "required": [],
    }

    async def execute(
        self, path: str | None = None, full_page: bool = False, **kwargs: Any
    ) -> str:
        thread_id, project_id = _execution_context(kwargs)
        if not path:
            return "Error: Screenshot destination was not materialized."
        from tools.host_access_gate import consume_current_action_permit

        consumed = await consume_current_action_permit(
            thread_id=thread_id,
            project_id=project_id,
            tool=self,
            tool_name=self.name,
            params={"path": path, "full_page": full_page},
        )
        if consumed is not None:
            if consumed.resolved_path is None:
                return "Error: reviewed screenshot destination is unavailable"
            save_path = consumed.resolved_path
            lease_id = consumed.lease_id
        else:
            try:
                save_path = await asyncio.to_thread(
                    _resolve_path, path, self._workspace, for_write=True,
                )
            except PermissionError as exc:
                return f"Error: {exc}"
            lease_id = None

        try:
            with private_mkdtemp(
                BROWSER_STAGING_DIR, prefix="corsia-browser-screenshot-"
            ) as stage_dir:
                stage_path = Path(stage_dir).resolve(strict=True) / "screenshot.png"
                args = ["screenshot", str(stage_path)]
                if full_page:
                    args.insert(1, "--full")
                result = await _run(
                    args,
                    thread_id=thread_id,
                    lease_id=lease_id,
                )
                if result.startswith("Error"):
                    return result
                copy_bound_file(stage_path, save_path)
                public_result = result.replace(str(stage_path), str(save_path))
                return f"Screenshot saved to: {save_path}\n{public_result}".strip()
        except (OSError, PermissionError) as exc:
            return f"Error: {exc}"

