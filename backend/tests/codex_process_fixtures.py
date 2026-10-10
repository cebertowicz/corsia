"""Process-family binding for tests whose OS subprocess is already synthetic."""

import signal
from unittest.mock import AsyncMock


def bind_process_group(monkeypatch, process):
    class Group:
        def send(self, selected_signal):
            if process.returncode is None:
                if selected_signal == signal.SIGKILL:
                    process.kill()
                else:
                    process.terminate()

        wait = AsyncMock()

    monkeypatch.setattr("subagents.codex_app_server.CodexProcessGroup", lambda _pid: Group())
