"""Isolated generic ONNX child process and a fail-closed synchronous parent."""

from __future__ import annotations

import multiprocessing
import os
import resource
import threading
import time
from multiprocessing.connection import Connection
from multiprocessing.context import SpawnProcess
from pathlib import Path
from typing import Any, Callable

import psutil

from core.onnx_asset import (
    AssetManifest,
    OnnxAssetSpec,
    check_self_test,
    load_pinned_manifest,
    load_tokenizer,
    open_session,
)

# Spawn, never the Linux default fork: a forked child inherits every open parent socket
# (listeners, keep-alive connections, websockets), so the peer never sees a FIN after the
# parent closes one while this long-lived child still holds a copy.
_SPAWN = multiprocessing.get_context("spawn")
Pipe = _SPAWN.Pipe
Process = _SPAWN.Process

RSS_POLL_SECONDS = 0.05
CLOSE_JOIN_SECONDS = 1.0


class OnnxUnavailable(RuntimeError):
    """The child did not become safely available to serve a request."""

    def __init__(self, reason: str, *, kind: str = "transient", cause: str | None = None) -> None:
        super().__init__(reason)
        self.kind = kind
        self.cause = cause


class OnnxMessageFailed(RuntimeError):
    """A ready child failed while serving one request."""

    def __init__(self, reason: str):
        super().__init__(reason)
        self.reason = reason


def child_main(connection: Any, manifest_path: str, spec: OnnxAssetSpec) -> None:
    """Own one verified session, deny network, and send a reply for each serial request."""
    try:
        _disable_child_network()
        import numpy as np
        import onnxruntime as ort

        manifest = load_pinned_manifest(Path(manifest_path).parent, spec)
        manifest.verify_files(spec)
        session = open_session(manifest, ort)
        tokenizer = load_tokenizer(manifest)
        check_self_test(manifest, spec, session, np, tokenizer)
        if _peak_rss_bytes() > spec.rss_limit_bytes:
            connection.send({"error": "rss", "kind": "deterministic"})
            return
        connection.send({"ready": True})
        while connection.poll(spec.idle_seconds):
            request = connection.recv()
            if request == {"op": "close"}:
                return
            score, truncated, original = spec.score(session, np, tokenizer, request)
            if _peak_rss_bytes() > spec.rss_limit_bytes:
                connection.send({"error": "rss"})
                return
            connection.send({
                "score": score,
                "version": manifest.head_version(str(request.get("head", spec.required_heads[0]))),
                "truncated": truncated,
                "original_tokens": original,
            })
    except Exception as error:
        kind = "transient" if isinstance(error, (OSError, MemoryError, TimeoutError)) else "deterministic"
        connection.send({"error": f"error:{type(error).__name__}", "kind": kind})


class OnnxChild:
    """Serial parent for a short-lived ONNX child with an enforced RSS ceiling."""

    def __init__(
        self,
        manifest: AssetManifest,
        spec: OnnxAssetSpec,
        *,
        target: Callable[..., None] = child_main,
        target_args: tuple[Any, ...] = (),
    ) -> None:
        self._manifest = manifest
        self._spec = spec
        self._target = target
        self._target_args = target_args
        self._lock = threading.Lock()
        self._process: SpawnProcess | None = None
        self._connection: Connection[Any, Any] | None = None
        self._killed = False
        self.loaded = False
        self.last_used = 0.0

    @property
    def running(self) -> bool:
        """Whether a child process exists and is alive."""
        process = self._process
        return process is not None and process.is_alive()

    def ensure_loaded(self) -> None:
        """Start and self-test the child; unsafe startup fails deterministically when appropriate."""
        with self._lock:
            if self.loaded and self.running:
                return
            self._start()
            reply = self._exchange(None, self._spec.load_timeout_seconds)
            if "error" in reply:
                raise OnnxUnavailable(
                    f"load {reply['error']}",
                    kind=str(reply.get("kind", "transient")),
                    cause=str(reply["cause"]) if "cause" in reply else None,
                )
            self.loaded = True

    def score(self, request: dict[str, Any]) -> dict[str, Any]:
        """Run one request after a passed self-test, raising a bounded per-message failure."""
        with self._lock:
            if not (self.loaded and self.running):
                raise OnnxUnavailable("not loaded")
            reply = self._exchange(request, self._spec.call_timeout_seconds)
            if "error" in reply:
                error = str(reply["error"])
                raise OnnxMessageFailed(error if error in ("timeout", "rss") else "child_error")
            self.last_used = time.monotonic()
            return reply

    def kill(self) -> None:
        """Stop the child immediately, including during a pending request."""
        self._killed = True
        process = self._process
        if process is not None and process.is_alive():
            process.kill()

    def close(self, *, lock_timeout: float = 10.0) -> None:
        """Request exit, then escalate until no live child handle remains."""
        if not self._lock.acquire(timeout=lock_timeout):
            self.kill()
            process = self._process
            if process is not None:
                process.join(lock_timeout)
            return
        try:
            self._stop(graceful=True)
        finally:
            self._lock.release()

    def _start(self) -> None:
        if self._killed:
            raise OnnxUnavailable("closed")
        self._stop(graceful=False)
        parent, child = Pipe()
        process = Process(
            target=self._target,
            args=(child, str(self._manifest.path), self._spec, *self._target_args),
            daemon=True,
        )
        process.start()
        child.close()
        self._process, self._connection, self.loaded = process, parent, False
        if self._killed:
            process.kill()

    def _exchange(self, request: dict[str, Any] | None, timeout: float) -> dict[str, Any]:
        """Send a request and poll both reply and RSS until one terminal result occurs."""
        connection = self._connection
        if connection is None or self._killed:
            raise OnnxUnavailable("closed")
        try:
            if request is not None:
                self._check_rss(request)
                connection.send(request)
            deadline = time.monotonic() + timeout
            while True:
                self._check_rss(request)
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    reply = {"error": "timeout", "kind": "transient"}
                    break
                if connection.poll(min(RSS_POLL_SECONDS, remaining)):
                    reply = connection.recv()
                    break
        except (EOFError, OSError):
            reply = {"error": "child_exited", "kind": "transient", "cause": "child_exited"}
        if self._killed:
            raise OnnxUnavailable("closed")
        if "error" in reply:
            self._stop(graceful=False)
        return reply

    def _check_rss(self, request: dict[str, Any] | None) -> None:
        try:
            rss = _child_rss_bytes(self._process)
        except psutil.NoSuchProcess:
            return  # an exited child holds no memory; its closed pipe reports child_exited
        except Exception as error:
            # An unreadable meter is the parent's environment, not this input or asset version.
            self._stop(graceful=False)
            raise OnnxUnavailable("rss_unavailable", cause=type(error).__name__) from error
        if rss > self._spec.rss_limit_bytes:
            self._stop(graceful=False)
            if request is None:
                raise OnnxUnavailable("load rss", kind="deterministic")
            raise OnnxMessageFailed("rss")

    def _stop(self, *, graceful: bool) -> None:
        process, connection = self._process, self._connection
        if process is None:
            return
        if graceful and process.is_alive() and connection is not None:
            try:
                connection.send({"op": "close"})
            except (OSError, EOFError):
                pass
            process.join(CLOSE_JOIN_SECONDS)
        for escalate in (process.terminate, process.kill):
            if not process.is_alive():
                break
            escalate()
            process.join(CLOSE_JOIN_SECONDS)
        if process.is_alive():
            raise OnnxUnavailable("child_did_not_exit")
        process.join()
        if connection is not None:
            connection.close()
        self._process, self._connection, self.loaded = None, None, False


def _child_rss_bytes(process: SpawnProcess | None) -> int:
    """Read the child RSS or fail rather than permit an uncapped process to continue."""
    if process is None:
        raise RuntimeError("child has no process")
    return psutil.Process(process.pid).memory_info().rss


def _peak_rss_bytes() -> int:
    value = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss
    return value if os.uname().sysname == "Darwin" else value * 1024


def _disable_child_network() -> None:
    """Reject outbound sockets before loading ONNX dependencies and disable ORT telemetry."""
    import socket

    def blocked_connect(*_args: Any, **_kwargs: Any) -> None:
        raise OSError("ONNX asset child prohibits outbound connections")

    setattr(socket.socket, "connect", blocked_connect)
    os.environ["ORT_DISABLE_TELEMETRY_EVENTS"] = "1"
