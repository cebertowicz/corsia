"""Batched reading and validation of immutable recorded guidance blobs."""
from __future__ import annotations

import hashlib
import os
import re
import subprocess
import threading
from pathlib import Path
from typing import IO

from core.guidance_manifest import git_blob_oid
from infra.guidance_git import GitFault, MAX_GUIDANCE_BYTES

_UNAVAILABLE = "exact recorded file object unavailable"
_UNAVAILABLE_OR_LIMIT = "exact recorded file object unavailable or exceeds guidance limit"


def _verified(data: bytes, oid: str, raw_sha256: object) -> tuple[bytes | None, str | None]:
    if git_blob_oid(data) != oid or hashlib.sha256(data).hexdigest() != raw_sha256:
        return None, "recorded file identity mismatch"
    try:
        data.decode("utf-8")
    except UnicodeDecodeError:
        return None, "exact recorded file object unreadable"
    return data, None


class BatchBlobReader:
    """Serve every recorded read of one render from one ``git cat-file --batch`` process.

    ``--batch-command`` (Git 2.36) asks for object metadata before requesting blob content, so
    a non-blob or over-limit object never forces a second process merely to realign its output
    stream. The process starts on the first valid object id, restarts after an actual fault, and
    ends at ``close()``; the caller owns that lifecycle.
    """

    @classmethod
    def for_repo(cls, repo: Path, git_config: tuple[str, ...], timeout: float) -> "BatchBlobReader":
        command = ["git", "-C", str(repo), *git_config, "cat-file", "--batch-command", "--buffer"]
        return cls(command, {**os.environ, "GIT_TERMINAL_PROMPT": "0"}, timeout)

    def __init__(self, command: list[str], env: dict[str, str], timeout: float) -> None:
        self._command, self._env, self._timeout = command, env, timeout
        self._proc: subprocess.Popen | None = None
        self.spawned = 0

    def __call__(self, oid: object, raw_sha256: object) -> tuple[bytes | None, str | None]:
        if not isinstance(oid, str) or re.fullmatch(r"[0-9a-f]{40}", oid) is None:
            return None, _UNAVAILABLE
        try:
            kind, data = self._read(oid)
        except (GitFault, ValueError):
            self.close()
            return None, _UNAVAILABLE_OR_LIMIT
        if kind is None:
            return None, _UNAVAILABLE_OR_LIMIT
        if kind != "blob":
            return None, _UNAVAILABLE
        if data is None:
            return None, _UNAVAILABLE_OR_LIMIT
        return _verified(data, oid, raw_sha256)

    def close(self) -> None:
        proc, self._proc = self._proc, None
        if proc is None:
            return
        try:
            proc.kill()
        except OSError:  # already exited, e.g. killed by the per-read watchdog
            pass
        for stream in (proc.stdin, proc.stdout):
            if stream is None:  # never had that pipe, so there is nothing to close
                continue
            try:
                stream.close()
            except OSError:
                pass
        proc.wait()

    def _open(self) -> subprocess.Popen:
        if self._proc is None:
            try:
                self._proc = subprocess.Popen(
                    self._command, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                    stderr=subprocess.DEVNULL, env=self._env)
            except (OSError, subprocess.SubprocessError) as exc:
                raise GitFault(f"git cat-file: {exc}") from exc
            self.spawned += 1
        return self._proc

    def _read(self, oid: str) -> tuple[str | None, bytes | None]:
        """``(None, None)`` for missing objects; request content only for a bounded blob."""
        proc = self._open()
        # The process starts with both pipes, so a missing one is a fault, never a fallback.
        stdin, stdout = proc.stdin, proc.stdout
        if stdin is None or stdout is None:
            raise GitFault("git cat-file: batch process has no pipes")
        killed = threading.Event()
        watchdog = threading.Timer(self._timeout, self._on_timeout, (proc, killed))
        watchdog.start()
        try:
            header = self._batch_command(stdin, stdout, "info", oid)
            if len(header) == 2 and header[1] == b"missing":
                return None, None
            if len(header) != 3 or header[0].decode() != oid:
                raise GitFault("git cat-file: unexpected batch response")
            kind, size = header[1].decode(), int(header[2])
            if kind != "blob" or not 0 <= size <= MAX_GUIDANCE_BYTES:
                return kind, None
            content_header = self._batch_command(stdin, stdout, "contents", oid)
            if content_header != header:
                raise GitFault("git cat-file: unexpected content response")
            data = self._take(stdout, size)
            if stdout.read(1) != b"\n":
                raise GitFault("git cat-file: truncated batch response")
            return kind, data
        except OSError as exc:
            raise GitFault(f"git cat-file: {exc}") from exc
        finally:
            watchdog.cancel()
            watchdog.join()  # no in-flight kill outlives this read to hit a reused process
            if killed.is_set() and self._proc is proc:
                # The watchdog did fire, maybe only after this read took its last byte, so the
                # result stands while the process is doomed. Uncache and reap it: the next read
                # starts a fresh one instead of faulting over a corpse.
                self.close()

    @staticmethod
    def _batch_command(stdin: IO[bytes], stdout: IO[bytes], command: str, oid: str) -> list[bytes]:
        """Send one flushed ``--batch-command`` request and return its metadata line."""
        stdin.write(command.encode() + b" " + oid.encode() + b"\nflush\n")
        stdin.flush()
        return stdout.readline().split()

    def _on_timeout(self, proc: subprocess.Popen, killed: threading.Event) -> None:
        """Mark the firing before killing, so the read can tell a watchdog that fired from one it cancelled."""
        killed.set()
        self._kill(proc)

    @staticmethod
    def _kill(proc: subprocess.Popen) -> None:
        try:
            proc.kill()
        except OSError:  # already exited
            pass

    @staticmethod
    def _take(stdout: IO[bytes], size: int) -> bytes:
        """Consume exactly one blob body from the batch process's stdout."""
        chunks, left = [], size
        while left:
            chunk = stdout.read(min(left, 1 << 20))
            if not chunk:
                raise GitFault("git cat-file: truncated batch response")
            left -= len(chunk)
            chunks.append(chunk)
        return b"".join(chunks)
