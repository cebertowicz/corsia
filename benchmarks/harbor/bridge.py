"""Unix-socket JSON bridge that confines CORSIA solver effects to Harbor."""

from __future__ import annotations

import asyncio
import json
import uuid
from pathlib import Path
from typing import Any, Protocol

MAX_READ_BYTES = 256_000


class HarborEnvironment(Protocol):
    """The small Harbor environment surface the bridge is allowed to use."""

    async def exec(self, command: str, **kwargs: Any) -> Any: ...

    async def upload_file(self, source_path: Path | str, target_path: str) -> Any: ...

    async def download_file(self, source_path: str, target_path: Path | str) -> Any: ...


class BridgeError(RuntimeError):
    """A malformed or unsupported bridge request."""


class HarborBridge:
    """Serve explicit container operations over one task-scoped Unix socket.

    Host-side file effects are limited to a bridge-owned staging directory:
    ``upload`` and ``download`` name a staging file, never an arbitrary host path.
    """

    def __init__(
        self,
        environment: HarborEnvironment,
        socket_path: Path,
        trace_path: Path,
        staging_dir: Path | None = None,
    ) -> None:
        self._environment = environment
        self._socket_path = socket_path
        self._trace_path = trace_path
        self._staging_dir = staging_dir or trace_path.parent / "bridge-staging"
        self._server: asyncio.AbstractServer | None = None

    async def start(self) -> None:
        """Start a task-local socket, replacing no pre-existing path."""
        if self._socket_path.exists():
            raise BridgeError(f"bridge socket already exists: {self._socket_path}")
        self._socket_path.parent.mkdir(parents=True, exist_ok=True)
        self._trace_path.parent.mkdir(parents=True, exist_ok=True)
        self._staging_dir.mkdir(parents=True, exist_ok=True)
        self._server = await asyncio.start_unix_server(self._handle, path=str(self._socket_path))

    async def stop(self) -> None:
        """Close the bridge and remove only its task-local socket."""
        if self._server is not None:
            self._server.close()
            await self._server.wait_closed()
            self._server = None
        self._socket_path.unlink(missing_ok=True)

    async def _handle(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        try:
            line = await reader.readline()
            request = json.loads(line)
            if not isinstance(request, dict):
                raise BridgeError("bridge request must be a JSON object")
            result = await self._dispatch(request)
            payload = {"ok": True, "result": result}
        except (BridgeError, json.JSONDecodeError, ValueError, OSError) as exc:
            payload = {"ok": False, "error": str(exc)}
        writer.write((json.dumps(payload, sort_keys=True) + "\n").encode())
        await writer.drain()
        writer.close()
        await writer.wait_closed()

    async def _dispatch(self, request: dict[str, Any]) -> Any:
        operation = str(request.get("operation"))
        handler = {
            "exec": self._exec,
            "read": self._read,
            "write": self._write,
            "upload": self._upload,
            "download": self._download,
        }.get(operation)
        if handler is None:
            raise BridgeError(f"unsupported bridge operation: {operation!r}")
        result, trace = await handler(request)
        self._append_trace({"operation": operation, **trace})
        return result

    async def _exec(self, request: dict[str, Any]) -> tuple[str, dict[str, Any]]:
        command = _required_str(request, "command")
        cwd = request.get("cwd")
        timeout = request.get("timeout")
        if cwd is not None and not isinstance(cwd, str):
            raise BridgeError("cwd must be a string")
        if timeout is not None and (not isinstance(timeout, (int, float)) or timeout <= 0):
            raise BridgeError("timeout must be positive")
        timeout_sec = None if timeout is None else max(1, int(timeout))
        result = await self._environment.exec(command, cwd=cwd, timeout_sec=timeout_sec)
        return _format_exec(result), {"command": command, "cwd": cwd, "timeout": timeout}

    async def _read(self, request: dict[str, Any]) -> tuple[str, dict[str, Any]]:
        path = _container_path(request)
        local = self._scratch_path()
        try:
            await self._environment.download_file(path, local)
            data = local.read_bytes()
        finally:
            local.unlink(missing_ok=True)
        if len(data) > MAX_READ_BYTES:
            raise BridgeError(f"{path} is {len(data)} bytes; read limit is {MAX_READ_BYTES}")
        return data.decode("utf-8", errors="replace"), {"path": path, "bytes": len(data)}

    async def _write(self, request: dict[str, Any]) -> tuple[str, dict[str, Any]]:
        path = _container_path(request)
        content = request.get("content")
        if not isinstance(content, str):
            raise BridgeError("write requires string content")
        local = self._scratch_path()
        try:
            local.write_text(content, encoding="utf-8")
            await self._environment.upload_file(local, path)
        finally:
            local.unlink(missing_ok=True)
        size = len(content.encode("utf-8"))
        return f"wrote {size} bytes to {path}", {"path": path, "bytes": size}

    async def _upload(self, request: dict[str, Any]) -> tuple[str, dict[str, Any]]:
        path = _container_path(request)
        local = self._staging_file(request)
        if not local.is_file():
            raise BridgeError(f"staging file does not exist: {local.name}")
        await self._environment.upload_file(local, path)
        return f"uploaded {local.name} to {path}", {"path": path, "local_name": local.name}

    async def _download(self, request: dict[str, Any]) -> tuple[str, dict[str, Any]]:
        path = _container_path(request)
        local = self._staging_file(request)
        await self._environment.download_file(path, local)
        return f"downloaded {path} to staging {local.name}", {"path": path, "local_name": local.name}

    def _scratch_path(self) -> Path:
        self._staging_dir.mkdir(parents=True, exist_ok=True)
        return self._staging_dir / f".scratch-{uuid.uuid4().hex}"

    def _staging_file(self, request: dict[str, Any]) -> Path:
        name = _required_str(request, "local_name")
        if name != Path(name).name or name.startswith("."):
            raise BridgeError("local_name must be a plain staging file name")
        self._staging_dir.mkdir(parents=True, exist_ok=True)
        return self._staging_dir / name

    def _append_trace(self, row: dict[str, Any]) -> None:
        with self._trace_path.open("a", encoding="utf-8") as trace:
            trace.write(json.dumps(row, sort_keys=True) + "\n")


def _required_str(request: dict[str, Any], key: str) -> str:
    value = request.get(key)
    if not isinstance(value, str) or not value:
        raise BridgeError(f"{request.get('operation')} requires a non-empty {key}")
    return value


def _container_path(request: dict[str, Any]) -> str:
    path = _required_str(request, "path")
    if not path.startswith("/"):
        raise BridgeError("path must be an absolute container path")
    return path


def _format_exec(result: Any) -> str:
    if hasattr(result, "return_code"):
        return json.dumps({
            "return_code": result.return_code,
            "stdout": getattr(result, "stdout", None),
            "stderr": getattr(result, "stderr", None),
        })
    return str(result)
