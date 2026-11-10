"""Launch a local MCP app over stdio, keeping a short masked tail of its stderr for the log."""

import asyncio
from collections import deque
from collections.abc import Iterable
from contextlib import AsyncExitStack
import os
import sys
import threading
from types import TracebackType

from loguru import logger

from host_access.credential_sanitizer import REDACTION_MARKER, is_credential_name, sanitize_credential_text
from tools.mcp_config import MCPConfigError
from tools.mcp_local_transport import mcp_oserror_detail, safehouse_stdio_parameters

TAIL_LINES = 20
LINE_CHARS = 200
# Lines are masked together, before each is cut to LINE_CHARS, so a key crossing the cut
# or a key block spanning lines is still recognised; bytes past this point of a line can
# never reach its kept part.
_LINE_BYTES = LINE_CHARS * 4 + 4096


class StderrTail:
    """Relay a child's stderr to ours unchanged, keeping its last lines in memory."""

    def __init__(self, name: str, secrets: Iterable[str]):
        self.name = name
        self._secrets = sorted({value for value in secrets if value}, key=len, reverse=True)
        self._lines: deque[str] = deque(maxlen=TAIL_LINES)
        self._lock = threading.Lock()
        read_fd, write_fd = os.pipe()
        # What the SDK hands the child as its stderr; the child gets its own copy at spawn.
        self.file = open(write_fd, "w", encoding="utf-8", closefd=True)
        try:
            forward: int | None = sys.stderr.fileno()
        except (AttributeError, OSError, ValueError):
            forward = None
        self._reader = threading.Thread(
            target=self._relay, args=(read_fd, forward), name=f"mcp-stderr:{name}", daemon=True,
        )
        self._reader.start()

    def _relay(self, read_fd: int, forward: int | None) -> None:
        partial = b""
        with open(read_fd, "rb", buffering=0, closefd=True) as pipe:
            while chunk := pipe.read(4096):
                if forward is not None:
                    forward = _write_all(forward, chunk)
                *complete, partial = (partial + chunk).split(b"\n")
                partial = partial[:_LINE_BYTES]
                self._keep(line[:_LINE_BYTES] for line in complete)
        if partial:
            self._keep([partial])

    def _keep(self, lines: Iterable[bytes]) -> None:
        decoded = [line.decode("utf-8", "replace").rstrip("\r") for line in lines]
        with self._lock:
            self._lines.extend(decoded)

    def mask(self, text: str) -> str:
        """``text`` without any key CORSIA gave the app, stored grant, or credential-shaped value."""
        from tools.mcp_oauth_secrets import redact

        for secret in self._secrets:
            text = text.replace(secret, REDACTION_MARKER)
        return sanitize_credential_text(redact(text))

    def lines(self) -> list[str]:
        """The last lines, masked, each cut to LINE_CHARS."""
        with self._lock:
            raw = list(self._lines)
        if not raw:
            return []
        return [line[:LINE_CHARS] for line in self.mask("\n".join(raw)).split("\n")]

    async def close(
        self, exc_type: type[BaseException] | None, exc: BaseException | None, tb: TracebackType | None,
    ) -> bool:
        """Exit callback: close our end, and log the tail if the connection unwound with an error."""
        self.file.close()
        # The child is gone by now, so the reader is at end of file unless something it
        # started still holds the pipe; never wait long for that.
        await asyncio.to_thread(self._reader.join, 1.0)
        if exc_type is not None:
            # Name what ended the connection: it may be CORSIA rejecting a healthy app or
            # cancelling its start, not the app failing.
            reason = self.mask(_describe(exc_type, exc))[:LINE_CHARS]
            lines = self.lines()
            if lines:
                logger.warning(
                    "MCP server '{}' connection ended by {}; its last {} stderr lines (keys masked):\n{}",
                    self.name, reason, len(lines), "\n".join(lines),
                )
            else:
                logger.warning(
                    "MCP server '{}' connection ended by {}; it wrote nothing to stderr", self.name, reason,
                )
        return False


def _describe(exc_type: type[BaseException], exc: BaseException | None) -> str:
    """``Type: message`` of the error, looking through task groups that wrap a single one."""
    while isinstance(exc, BaseExceptionGroup) and len(exc.exceptions) == 1:
        exc = exc.exceptions[0]
    if exc is None:
        return exc_type.__name__
    return f"{type(exc).__name__}: {exc}" if str(exc) else type(exc).__name__


def _write_all(fd: int, data: bytes) -> int | None:
    """Forward ``data`` to ``fd``; ``None`` once it can no longer be written."""
    view = memoryview(data)
    try:
        while view:
            view = view[os.write(fd, view):]
    except OSError:
        return None
    return fd


async def open_stdio(name: str, config: dict, secret_env: dict[str, str], stack: AsyncExitStack):
    """Start a local app under Safehouse; the stack owns the process and the stderr tail."""
    from mcp.client.stdio import stdio_client

    configured = config.get("env") or {}
    env = {**configured, **secret_env}
    params, profile_path = await asyncio.to_thread(
        safehouse_stdio_parameters, name, config, config["command"], config.get("args", []), env,
    )
    stack.callback(profile_path.unlink, missing_ok=True)
    secrets = [*secret_env.values(), *(value for key, value in configured.items() if is_credential_name(key))]
    tail = StderrTail(name, (value for value in secrets if isinstance(value, str)))
    stack.push_async_exit(tail.close)
    try:
        return await stack.enter_async_context(stdio_client(params, errlog=tail.file))
    except OSError as error:
        raise MCPConfigError(mcp_oserror_detail(error)) from None
