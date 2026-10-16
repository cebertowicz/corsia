"""The isolated model process for the corrections classifier and its synchronous parent.

The child owns one ONNX session, refuses network access, checks its memory ceiling and
answers one request at a time. The parent separates two kinds of failure:

- ``ClassifierUnavailable``: a retryable outage. The model never became ready (load timeout,
  a native exit before any reply) or the parent was shut down. Nothing is recorded; the message
  stays pending. Deterministic startup failures (a self-test mismatch or a load-time memory-ceiling
  breach) are marked for version exclusion instead. A caller may exclude repeated native exits
  only when another verified version can serve; otherwise it keeps the version retryable and
  rate-limits later load attempts.
- ``MessageFailed``: a child that loaded and passed its self-test failed on this input
  (``timeout``, ``rss`` or ``child_error``). The child is stopped; the next call starts a
  fresh one.
"""

from __future__ import annotations

import os
import resource
import threading
import time
from multiprocessing import Pipe, Process
from multiprocessing.connection import Connection
from pathlib import Path
from typing import Any, Callable

from core.corrections_classifier import (
    AssetManifest, Classification, check_self_test, load_tokenizer, open_session, score_input,
    load_pinned_manifest,
)
from core.corrections_recording import ClassifierVersion

RSS_LIMIT_BYTES = int(2.5 * 1024**3)
CALL_TIMEOUT_SECONDS = 30.0
LOAD_TIMEOUT_SECONDS = 180.0  # cold start: verify, load and self-test, apart from the per-call bound
IDLE_SECONDS = 10 * 60
CLOSE_JOIN_SECONDS = 1.0


class ClassifierUnavailable(RuntimeError):
    """The model could not load or answer at all; the message stays pending."""

    def __init__(self, reason: str, *, kind: str = "transient", cause: str | None = None) -> None:
        super().__init__(reason)
        self.kind = kind
        self.cause = cause


class MessageFailed(RuntimeError):
    """A loaded, self-tested model failed on this input: ``timeout``, ``rss`` or ``child_error``."""

    def __init__(self, reason: str):
        super().__init__(reason)
        self.reason = reason


def _peak_rss_bytes() -> int:
    value = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss
    return value if os.uname().sysname == "Darwin" else value * 1024


def _disable_child_network() -> None:
    """Disable ORT telemetry and reject TCP connects made through ``socket.socket.connect`` before imports."""
    import socket

    def blocked_connect(*_args, **_kwargs):
        raise OSError("corrections classifier child prohibits outbound connections")

    setattr(socket.socket, "connect", blocked_connect)
    os.environ["ORT_DISABLE_TELEMETRY_EVENTS"] = "1"


def _child_main(connection, manifest_path: str) -> None:
    """Own one ORT session. It exits after a ceiling breach, never above it."""
    try:
        _disable_child_network()
        import numpy as np
        import onnxruntime as ort

        manifest = load_pinned_manifest(Path(manifest_path).parent)
        manifest.verify_files()
        session = open_session(manifest, ort)
        tokenizer = load_tokenizer(manifest)
        check_self_test(manifest, session, np, tokenizer)
        if _peak_rss_bytes() > RSS_LIMIT_BYTES:
            connection.send({"error": "rss", "kind": "deterministic"})
            return
        connection.send({"ready": True})
        while connection.poll(IDLE_SECONDS):
            request = connection.recv()
            if request == {"op": "close"}:
                return
            version = manifest.classifier_version(str(request.get("head", "correction")))
            score, truncated, original = score_input(session, np, tokenizer, request["context"], request["owner"])
            if _peak_rss_bytes() > RSS_LIMIT_BYTES:
                connection.send({"error": "rss"})
                return
            connection.send({
                "score": score, "version": version.__dict__, "truncated": truncated,
                "original_tokens": original,
            })
    except Exception as error:
        kind = "transient" if isinstance(error, (OSError, MemoryError, TimeoutError)) else "deterministic"
        connection.send({"error": f"error:{type(error).__name__}", "kind": kind})


class ClassifierChild:
    """Synchronous parent for the short-lived, serial model child process.

    One lock serializes every use of the process and its pipe. ``kill`` deliberately skips
    the lock so shutdown can stop a child that a worker thread is still waiting on; the
    waiting thread then reads end-of-file at once and raises ``ClassifierUnavailable``.
    """

    def __init__(self, manifest: AssetManifest, *, target: Callable[..., None] = _child_main):
        self._manifest = manifest
        self._target = target
        self._lock = threading.Lock()
        self._process: Process | None = None
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
        """Start the child if needed and wait for its passed self-test; an outage raises."""
        with self._lock:
            if self.loaded and self.running:
                return
            self._start()
            reply = self._exchange(None, LOAD_TIMEOUT_SECONDS)
            if "error" in reply:
                cause = reply.get("cause")
                raise ClassifierUnavailable(
                    f"load {reply['error']}", kind=str(reply.get("kind", "transient")),
                    cause=str(cause) if cause is not None else None,
                )
            self.loaded = True

    def classify(self, context: list[tuple[str, str]], owner: str, *, head: str = "correction") -> Classification:
        """Score one bounded input on the loaded child; a failure on this input raises MessageFailed."""
        with self._lock:
            if not (self.loaded and self.running):
                raise ClassifierUnavailable("not loaded")
            reply = self._exchange({"context": list(context), "owner": owner, "head": head}, CALL_TIMEOUT_SECONDS)
            if "error" in reply:
                error = str(reply["error"])
                raise MessageFailed(error if error in ("timeout", "rss") else "child_error")
            self.last_used = time.monotonic()
        version = ClassifierVersion(**reply["version"])
        return Classification(
            version=version,
            score=float(reply["score"]),
            label="correction" if float(reply["score"]) >= version.threshold else "not_correction",
            truncated=bool(reply["truncated"]),
            original_tokens=int(reply["original_tokens"]),
        )

    def kill(self) -> None:
        """Stop the process now, even mid-call; this child then refuses all further use."""
        self._killed = True
        process = self._process
        if process is not None and process.is_alive():
            process.kill()

    def close(self, *, lock_timeout: float = 10.0) -> None:
        """Ask the child to exit, then escalate until it has; a live handle is never dropped."""
        if not self._lock.acquire(timeout=lock_timeout):
            # A worker thread still holds the pipe: only the process may be touched.
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
            raise ClassifierUnavailable("closed")
        self._stop(graceful=False)
        parent, child = Pipe()
        process = Process(target=self._target, args=(child, str(self._manifest.path)), daemon=True)
        process.start()
        # Only the child may hold its end: then the child's exit reads as EOF here at once.
        child.close()
        self._process, self._connection, self.loaded = process, parent, False
        if self._killed:
            # Shutdown ran while this child was starting.
            process.kill()

    def _exchange(self, request: dict | None, timeout: float) -> dict:
        """Send the request, if any, and wait for one reply; any failure stops the child."""
        connection = self._connection
        if connection is None or self._killed:
            raise ClassifierUnavailable("closed")
        try:
            if request is not None:
                connection.send(request)
            reply = (connection.recv() if connection.poll(timeout)
                     else {"error": "timeout", "kind": "transient"})
        except (EOFError, OSError):
            reply = {"error": "child_exited", "kind": "transient", "cause": "child_exited"}
        if self._killed:
            raise ClassifierUnavailable("closed")
        if "error" in reply:
            self._stop(graceful=False)
        return reply

    def _stop(self, *, graceful: bool) -> None:
        """Close, then terminate, then kill, joining after each; clear handles only once dead."""
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
            raise ClassifierUnavailable("child_did_not_exit")
        process.join()
        if connection is not None:
            connection.close()
        self._process, self._connection, self.loaded = None, None, False
