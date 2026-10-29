"""Command invocation and result rendering for the browser session owner."""
from __future__ import annotations

import sys
from pathlib import Path
from typing import Any, Sequence

from host_access import browser_cli
from host_access.browser_session_state import BrowserSessionRecord


class BrowserSessionIO:
    """Use the session owner's identity when invoking its browser client."""

    socket_dir: Path
    _sessions: dict[str, Any]
    _chrome: str | None

    def _model_result(self, result: browser_cli.CommandResult, record: BrowserSessionRecord) -> str:
        return browser_cli.render_model_result(result, redactions={
            str(self.socket_dir): "[browser-runtime]", record.session: "[browser-session]",
            record.thread_id: "[browser-thread]", record.lease_id or "": "[browser-lease]",
        })

    async def _invoke(self, argv: Sequence[str], *, timeout: float) -> browser_cli.CommandResult:
        session = argv[argv.index("--session") + 1] if "--session" in argv else None
        runtime = next((item for item in self._sessions.values() if item.record.session == session), None)
        options = {"stream_port": runtime.chrome.stream_port} if runtime and runtime.chrome else {}
        if sys.platform == "linux":
            return await browser_cli.invoke(argv, socket_dir=self.socket_dir, timeout=timeout,
                                            config_path=self.socket_dir / "config.json",
                                            linux_chrome=self._chrome, **options)
        return await browser_cli.invoke(argv, socket_dir=self.socket_dir, timeout=timeout,
                                        config_path=self.socket_dir / "config.json", **options)
