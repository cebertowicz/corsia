"""Fetching one pinned runtime archive, and the errors every pinned-runtime caller classifies by type."""
from __future__ import annotations

import hashlib
import time
import urllib.request
from collections.abc import Callable
from pathlib import Path

# The largest pinned archive is far below this cap; the cap bounds a compromised or
# malformed response before it can hold the connected-app lifecycle lock indefinitely.
_MAX_DOWNLOAD_BYTES = 512 * 1024 * 1024
_DOWNLOAD_DEADLINE_SECONDS = 5 * 60


class ManagedRuntimeError(RuntimeError):
    """A download or install that must not use an unpinned fallback."""


class ManagedRuntimeDownloadError(ManagedRuntimeError):
    """A pinned runtime could not be fetched or verified."""


class ManagedRuntimeUnverifiedError(ManagedRuntimeError):
    """The download arrived but is not the pinned runtime: wrong bytes, oversized, or the wrong contents."""


class ManagedRuntimeChecksumError(ManagedRuntimeDownloadError, ManagedRuntimeUnverifiedError):
    """The downloaded bytes do not match the pin."""


class ManagedRuntimeCancelled(ManagedRuntimeError):
    """The caller asked the download to stop."""


def _network_failure(exc: BaseException) -> ManagedRuntimeDownloadError:
    """A fetch failure whose cause is the network error, so callers can classify it by type."""
    error = ManagedRuntimeDownloadError(f"download failed: {type(exc).__name__}")
    error.__cause__ = exc
    return error


def download(url: str, destination: Path, should_stop: Callable[[], bool] | None = None) -> str:
    """Fetch url into destination, returning its sha256.

    Only fetching raises ``ManagedRuntimeDownloadError``; a local write failure (a full
    disk) propagates as ``OSError``. ``should_stop`` is checked before each chunk.
    """
    digest = hashlib.sha256()
    deadline = time.monotonic() + _DOWNLOAD_DEADLINE_SECONDS
    received = 0
    with destination.open("xb") as output:
        try:
            response = urllib.request.urlopen(url, timeout=30)
        except OSError as exc:  # URLError, HTTPError and timeouts are all OSError
            raise _network_failure(exc) from exc
        with response:
            while True:
                if should_stop is not None and should_stop():
                    raise ManagedRuntimeCancelled("pinned runtime download was stopped")
                try:
                    chunk = response.read(1024 * 1024)
                except OSError as exc:
                    raise _network_failure(exc) from exc
                if not chunk:
                    break
                if time.monotonic() >= deadline:
                    raise _network_failure(TimeoutError("pinned runtime download exceeded its deadline"))
                received += len(chunk)
                if received > _MAX_DOWNLOAD_BYTES:
                    raise ManagedRuntimeUnverifiedError("pinned runtime download exceeded its byte limit")
                digest.update(chunk)
                output.write(chunk)
    return digest.hexdigest()
