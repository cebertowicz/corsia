"""A codex tool result we cannot place must not kill the codex session.

2026-08-15, production, immediately after a forced restart::

    CORSIA Codex app-server bridge error: ToolResultBindingError:
    provider tool call does not resolve to one authoritative Assistant Step

A codex tool result came back for a call whose declaring turn no longer
existed, so the Step lookup found nothing, raised, and the raise reached the
reader's ``except Exception`` — which kills the codex process. That ends the
turn with its remaining calls unanswered, and each of those authors the stub
that makes the next result unplaceable.

Claude Code got this isolation in #1080; codex did not, and it is the harness
that actually hit it. Same rule, same narrowness: only this exception, only
this message. Anything else still tears the reader down.
"""

from __future__ import annotations

import json

import pytest


class _Stdout:
    def __init__(self, lines: list[bytes]) -> None:
        self._lines = list(lines)

    async def readline(self) -> bytes:
        return self._lines.pop(0) if self._lines else b""


class _Proc:
    def __init__(self) -> None:
        self.returncode = None
        self.killed = False
        self.stdout = _Stdout(
            [
                json.dumps({"id": 1, "method": "tool"}).encode() + b"\n",
                json.dumps({"id": 2, "method": "after"}).encode() + b"\n",
                b"",
            ]
        )
        self.stderr = None

    def kill(self) -> None:
        self.killed = True

    async def wait(self) -> int:
        return 0


@pytest.mark.asyncio
async def test_unbindable_codex_result_does_not_kill_the_session():
    from core._tool_result_delivery_support import ToolResultBindingError
    from subagents.codex_app_server import CodexAppServerSession

    seen: list[dict] = []

    async def _handle(msg: dict) -> None:
        seen.append(msg)
        if msg.get("method") == "tool":
            raise ToolResultBindingError(
                "provider tool call does not resolve to one authoritative "
                "Assistant Step"
            )

    session = CodexAppServerSession.__new__(CodexAppServerSession)
    session.corsia_thread_id = "thread-codex"
    session._proc = _Proc()
    session._closing = True
    session._pending = {}
    session._pending_methods = {}
    session._tool_requests = None
    session._on_exit = None
    session._handle_message = _handle

    await session._read_loop()

    assert session._proc.killed is False, (
        "a binding failure must never terminate the codex session — that is "
        "the step that turns one unplaceable result into an errored turn"
    )
    assert [msg.get("method") for msg in seen] == ["tool", "after"], (
        "the reader must keep handling messages after the unplaceable one"
    )
