"""Launch a local MCP app over stdio, keeping a short masked tail of its stderr for the log."""

import asyncio
from collections import deque
from collections.abc import Iterable
from contextlib import AsyncExitStack
import os
from queue import Empty, Queue
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
_FORWARD_LINE_BYTES = 65536
_OVERSIZE_MARKER = "[REDACTED: stderr line over 65536 bytes]"


class StderrTail:
    """Relay masked child stderr without letting a slow destination block the child."""

    def __init__(self, name: str, secrets: Iterable[str]):
        self.name = name
        self._secrets = sorted({value for value in secrets if value}, key=len, reverse=True)
        self._lines: deque[str] = deque(maxlen=TAIL_LINES)
        self._lock = threading.Lock()
        self._forward_queue: Queue[bytes] = Queue(maxsize=128)
        self._reader_done = threading.Event()
        self._dropped_lines = 0
        read_fd, write_fd = os.pipe()
        self.file = open(write_fd, "w", encoding="utf-8", closefd=True)
        try:
            forward: int | None = sys.stderr.fileno()
        except (AttributeError, OSError, ValueError):
            forward = None
        self._writer = threading.Thread(
            target=self._forward, args=(forward,), name=f"mcp-stderr-forward:{name}", daemon=True,
        )
        self._reader = threading.Thread(
            target=self._relay, args=(read_fd,), name=f"mcp-stderr:{name}", daemon=True,
        )
        self._writer.start()
        self._reader.start()

    def _forward(self, fd: int | None) -> None:
        while not self._reader_done.is_set() or not self._forward_queue.empty():
            try:
                data = self._forward_queue.get(timeout=0.05)
            except Empty:
                continue
            if fd is not None:
                try:
                    fd = _write_all(fd, data)
                except Exception:
                    fd = None

    def _relay(self, read_fd: int) -> None:
        partial = b""
        tail_partial = b""
        overflow = False
        private_block = False

        def emit(line: bytes, *, newline: bool, truncated: bool = False) -> None:
            nonlocal private_block
            try:
                text = line.decode("utf-8", "replace")
                begins_key = "-----BEGIN " in text and "PRIVATE KEY-----" in text
                ends_key = "-----END " in text and "PRIVATE KEY-----" in text
                hidden = truncated or private_block or begins_key or any("\n" in secret for secret in self._secrets)
                private_block = (private_block or begins_key) and not ends_key
                safe = _OVERSIZE_MARKER if truncated else REDACTION_MARKER if hidden else self.mask(text)
                self._forward_queue.put_nowait((safe + ("\n" if newline else "")).encode("utf-8"))
            except Exception:
                # A broken sanitizer or full queue drops this output, never the child.
                self._dropped_lines += 1

        try:
            with open(read_fd, "rb", buffering=0, closefd=True) as pipe:
                while chunk := pipe.read(4096):
                    *complete, tail_partial = (tail_partial + chunk).split(b"\n")
                    tail_partial = tail_partial[:_LINE_BYTES]
                    self._keep(line[:_LINE_BYTES] for line in complete)
                    parts = chunk.split(b"\n")
                    for index, data in enumerate(parts):
                        ended = index < len(parts) - 1
                        if not overflow:
                            if len(partial) + len(data) >= _FORWARD_LINE_BYTES:
                                emit((partial + data)[:_FORWARD_LINE_BYTES], newline=True, truncated=True)
                                partial, overflow = b"", True
                            else:
                                partial += data
                        if ended:
                            if not overflow:
                                emit(partial, newline=True)
                            partial, overflow = b"", False
            if partial:
                emit(partial, newline=False)
            if tail_partial:
                self._keep([tail_partial])
        finally:
            self._reader_done.set()

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
        try:
            return [line[:LINE_CHARS] for line in self.mask("\n".join(raw)).split("\n")]
        except Exception:
            return [REDACTION_MARKER]

    async def close(
        self, exc_type: type[BaseException] | None, exc: BaseException | None, tb: TracebackType | None,
    ) -> bool:
        """Exit callback: close our end, and log the tail if the connection unwound with an error."""
        self.file.close()
        # The child is gone by now, so the reader is at end of file unless something it
        # started still holds the pipe; never wait long for that.
        await asyncio.to_thread(self._reader.join, 1.0)
        await asyncio.to_thread(self._writer.join, 1.0)
        if self._dropped_lines:
            logger.warning("MCP server '{}' stderr relay dropped {} lines", self.name, self._dropped_lines)
        if exc_type is not None:
            # Name what ended the connection: it may be CORSIA rejecting a healthy app or
            # cancelling its start, not the app failing.
            try:
                reason = self.mask(_describe(exc_type, exc))[:LINE_CHARS]
            except Exception:
                reason = REDACTION_MARKER
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
