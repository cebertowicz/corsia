"""Browser automation tools wrapping the agent-browser CLI."""

from __future__ import annotations

import asyncio
import json
from pathlib import Path
import tempfile
from typing import Any
from urllib.parse import unquote, urlsplit

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


class BrowserOpenTool(Tool):
    """Navigate the browser to a URL."""

    def __init__(self, workspace: Path | None = None) -> None:
        self._workspace = workspace

    name = "browser_open"
    description = (
        "Navigate to a URL in the browser. Opens a new browser session if none is active. "
        "Headless is the default for ordinary inspection; set show_window to open a visible window.\n\n"
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
                "description": "Open a visible window the user can watch; headless by default.",
            },
        },
        "required": ["url"],
    }

    async def execute(self, url: str, wait_for_load: bool = True,
                      show_window: bool = False, **kwargs: Any) -> str:
        thread_id, project_id = _execution_context(kwargs)
        effective_url = url
        reviewed_local_path = False
        lease_id: str | None = None
        split = urlsplit(url)
        if split.scheme.lower() == "file":
            if split.netloc not in {"", "localhost"}:
                return "Error: Remote file URLs are not supported."
            if split.query or split.fragment:
                return "Error: Local file URLs cannot include a query or fragment."
            raw_path = unquote(split.path)
            from tools.host_access_gate import consume_current_action_permit

            consumed = await consume_current_action_permit(
                thread_id=thread_id,
                project_id=project_id,
                tool=self,
                tool_name=self.name,
                params={
                    "url": url,
                    "wait_for_load": wait_for_load,
                    "show_window": show_window,
                },
            )
            if consumed is not None:
                if consumed.resolved_path is None:
                    return "Error: reviewed local browser path is unavailable"
                local_path = consumed.resolved_path
                reviewed_local_path = True
                lease_id = consumed.lease_id
            else:
                try:
                    local_path = await asyncio.to_thread(_resolve_path, raw_path, self._workspace)
                except PermissionError as exc:
                    return f"Error: {exc}"
            if reviewed_local_path:
                return await self._open_reviewed_local_file(
                    local_path,
                    thread_id=thread_id,
                    wait_for_load=wait_for_load,
                    show_window=show_window,
                    lease_id=lease_id,
                )
            effective_url = local_path.as_uri()

        # agent-browser's open-URL command waits for navigation inside the
        # command itself. Long-lived applications can therefore make it time out
        # before this wrapper gets a chance to honor wait_for_load=False.
        # Launch a neutral page first, then schedule navigation through eval so
        # callers control whether to wait for the destination.
        result = await _run(
            ["open", "about:blank"],
            thread_id=thread_id,
            show_window=show_window,
        )
        if result.startswith("Error"):
            return result

        navigation_result = await _run(
            [
                "eval",
                f"location.href = {json.dumps(effective_url)}; 'navigating'",
            ],
            thread_id=thread_id,
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

    async def _open_reviewed_local_file(
        self,
        local_path: Path,
        *,
        thread_id: str,
        wait_for_load: bool,
        show_window: bool,
        lease_id: str | None,
    ) -> str:
        if local_path.suffix.lower() in {".htm", ".html"}:
            return (
                "Error: reviewed host HTML cannot be opened safely because "
                "relative resources cannot be descriptor-bound. Copy the "
                "document and its resources into the workspace first."
            )
        try:
            with tempfile.TemporaryDirectory(
                prefix="corsia-browser-open-"
            ) as stage_dir:
                stage_path = (
                    Path(stage_dir).resolve(strict=True)
                    / f"document{local_path.suffix}"
                )
                copy_bound_file(local_path, stage_path)
                result = await _run(
                    ["open", stage_path.as_uri()],
                    thread_id=thread_id,
                    lease_id=lease_id,
                    show_window=show_window,
                )
                if result.startswith("Error"):
                    return result
                wait_result = await _run(
                    ["wait", "--load", "networkidle"],
                    thread_id=thread_id,
                    lease_id=lease_id,
                    timeout=15,
                )
                if wait_result.startswith("Error"):
                    return wait_result
                return (
                    f"{result}\n{wait_result}".strip()
                    if wait_for_load
                    else result
                )
        except (OSError, PermissionError) as exc:
            return f"Error: {exc}"


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
            with tempfile.TemporaryDirectory(
                prefix="corsia-browser-screenshot-"
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

