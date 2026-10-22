"""A Claude Code context probe asks for usage only after the model was selected.

The stub below answers like a CLI that finishes requests as they complete: it
reads usage the moment it is asked, and applies ``set_model`` a moment later.
Sent together, the usage it returns describes the model selected before.
"""
from __future__ import annotations

import asyncio
import json

import pytest

from connections import claude_discovery

WINDOWS = {"default": 200_000, "fable[1m]": 1_000_000}


class _Out:
    def __init__(self, queue: asyncio.Queue):
        self._queue = queue
        self._eof = False

    async def read(self, limit: int = -1) -> bytes:
        if limit != -1:
            return await self._next()
        chunks = []
        while chunk := await self._next():
            chunks.append(chunk)
        return b"".join(chunks)

    async def _next(self) -> bytes:
        if self._eof:
            return b""
        chunk = await self._queue.get()
        self._eof = chunk is None
        return b"" if chunk is None else chunk


class _Err:
    async def read(self, _limit: int = -1) -> bytes:
        return b""


class _In:
    def __init__(self, process: "_StubCli"):
        self._process = process

    def write(self, payload: bytes) -> None:
        for line in payload.splitlines():
            self._process.received(json.loads(line))

    async def drain(self) -> None:
        return None

    def close(self) -> None:
        self._process.finish()

    def is_closing(self) -> bool:
        return False


class _StubCli:
    """In-memory CLI: answers each request on its own schedule."""

    def __init__(self, *, ack_delay: float = 0.02, set_model: str = "answer"):
        self.returncode = 0
        self.model = "default"
        self.frames: list[str] = []
        self._ack_delay = ack_delay
        self._set_model = set_model
        self._queue: asyncio.Queue = asyncio.Queue()
        self._tasks: list[asyncio.Task] = []
        self.stdin = _In(self)
        self.stdout = _Out(self._queue)
        self.stderr = _Err()

    def _put(self, request_id: str, subtype: str = "success", **body) -> None:
        response = {"request_id": request_id, "subtype": subtype, **body}
        self._queue.put_nowait(json.dumps({"type": "control_response", "response": response}).encode() + b"\n")

    def received(self, frame: dict) -> None:
        request = frame["request"]
        self.frames.append(request["subtype"])
        if request["subtype"] == "set_model":
            self._tasks.append(asyncio.ensure_future(self._apply(frame)))
        elif request["subtype"] == "get_context_usage":
            self._put(frame["request_id"], response={"rawMaxTokens": WINDOWS[self.model]})
        elif request["subtype"] == "end_session":
            self.finish()

    async def _apply(self, frame: dict) -> None:
        await asyncio.sleep(self._ack_delay)
        if self._set_model == "silent":
            return
        if self._set_model == "reject":
            self._put(frame["request_id"], "error", error="model not available for this account")
            return
        self.model = frame["request"]["model"]
        self._put(frame["request_id"])

    def finish(self) -> None:
        async def _close() -> None:
            await asyncio.gather(*self._tasks)
            self._queue.put_nowait(None)

        asyncio.ensure_future(_close())

    async def wait(self) -> int:
        return self.returncode

    async def communicate(self, payload: bytes | None = None) -> tuple[bytes, bytes]:
        if payload is not None:
            self.stdin.write(payload)
        self.stdin.close()
        return await self.stdout.read(), b""

    def kill(self) -> None:
        self.returncode = -9


def _install(monkeypatch, cli: _StubCli) -> None:
    async def _spawn(*_cmd, **_kwargs):
        return cli

    monkeypatch.setattr(claude_discovery.asyncio, "create_subprocess_exec", _spawn)


@pytest.mark.asyncio
async def test_the_window_read_is_the_selected_models_not_the_previous_one(monkeypatch):
    cli = _StubCli()
    _install(monkeypatch, cli)

    usage = await claude_discovery._discover_claude_context("/bin/claude", "fable[1m]")

    assert usage["rawMaxTokens"] == WINDOWS["fable[1m]"]
    assert cli.frames == ["set_model", "get_context_usage", "end_session"]


@pytest.mark.asyncio
async def test_frames_sent_together_read_the_previous_model(monkeypatch):
    """What the probe used to do: the stub's usage answer outruns its set_model."""
    cli = _StubCli()
    _install(monkeypatch, cli)
    frames = [
        {"type": "control_request", "request_id": "corsia-set-model",
         "request": {"subtype": "set_model", "model": "fable[1m]"}},
        {"type": "control_request", "request_id": "corsia-context-usage",
         "request": {"subtype": "get_context_usage"}},
    ]

    stdout = await claude_discovery.run_claude_control("/bin/claude", frames, operation="context")

    assert b'"rawMaxTokens": 200000' in stdout


@pytest.mark.asyncio
async def test_a_refused_selector_names_the_reason_and_asks_for_no_usage(monkeypatch):
    cli = _StubCli(set_model="reject")
    _install(monkeypatch, cli)

    with pytest.raises(RuntimeError, match="rejected selector fable.*model not available for this account"):
        await claude_discovery._discover_claude_context("/bin/claude", "fable[1m]")

    assert "get_context_usage" not in cli.frames


@pytest.mark.asyncio
async def test_a_cli_that_exits_without_answering_ends_the_probe_with_what_it_said(monkeypatch):
    cli = _StubCli(set_model="silent")
    _install(monkeypatch, cli)

    async def _exit_after_set_model() -> None:
        while "set_model" not in cli.frames:
            await asyncio.sleep(0)
        cli.finish()

    exit_task = asyncio.ensure_future(_exit_after_set_model())
    with pytest.raises(RuntimeError, match="no context evidence for fable.*control responses seen: none"):
        await asyncio.wait_for(claude_discovery._discover_claude_context("/bin/claude", "fable[1m]"), 5)
    await exit_task
