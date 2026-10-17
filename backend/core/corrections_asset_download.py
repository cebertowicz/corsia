"""First-run download of the pinned corrections-classifier asset.

One flight fetches the files of the newest pinned version into a staging folder
beside the asset root, verifies every byte against the pinned manifest, swaps the
whole folder into place and self-tests it; the previous version serves until then.
The classifier runtime owns the asset's meaning; this module only delivers and
proves it. The release host is never
trusted: the manifest's sha256 comes from source (`PINNED_MANIFESTS`) and each file
is checked against that manifest by size and sha256 before it is installed.
"""

from __future__ import annotations

import functools
import logging
import os
import shutil
import threading
import time
from pathlib import Path
from typing import Any, Callable

import httpx

from core.corrections_asset_files import (
    asset_root, free_bytes, newest_pin, previous_dir, recover_interrupted_install, root_is_newest_pin, sha256,
    short_of_space, staging_dir,
)
from core.corrections_classifier import CORRECTIONS_SPEC
from core.onnx_asset import AssetError, AssetManifest, load_manifest, run_self_test
from core.corrections_asset_pins import PINNED_MANIFESTS
from core.corrections_asset_signals import State, broadcast_update, public_state, wake_corrections

logger = logging.getLogger(__name__)

# The one place the release location lives; the environment override is for disposable proofs.
DEFAULT_BASE_URL = (
    "https://github.com/cebertowicz/corsia/releases/download/asset-corrections-classifier-v1"
)
BASE_URL_ENV = "CORSIA_CORRECTIONS_ASSET_BASE_URL"

DISK_MARGIN_BYTES = 256 * 1024**2
CONNECT_TIMEOUT_SECONDS = 15.0
READ_TIMEOUT_SECONDS = 30.0
RETRY_ATTEMPTS = 5
RETRY_BACKOFF_SECONDS = (1.0, 3.0, 8.0, 20.0, 45.0)
SPACE_RECHECK_SECONDS = 60.0

NOTIFY_MIN_SECONDS = 1.0

class _Failure(Exception):
    def __init__(self, code: str, reason: str, *, retryable: bool = True, **extra: Any):
        super().__init__(reason)
        self.state: State = {"state": "failed", "code": code, "reason": reason, "retryable": retryable, **extra}


def base_url() -> str:
    return (os.environ.get(BASE_URL_ENV) or DEFAULT_BASE_URL).rstrip("/")


def _reader(error: Exception) -> str:
    """The one sentence a person sees for a failed check: the asset's own wording names what it is, so it stays in the log."""
    logger.info("Corrections classifier check failed: %s", getattr(error, "reader", None) or error)
    return "The download didn’t pass its checks."


class ClassifierDownload:
    """The single in-process flight and its observable state."""

    def __init__(
        self,
        *,
        root: Callable[[], Path] = asset_root,
        url: Callable[[], str] = base_url,
        self_test: Callable[[Path], None] = functools.partial(run_self_test, spec=CORRECTIONS_SPEC),
        free_bytes: Callable[[Path], int] = free_bytes,
        sleep: Callable[[float], None] = time.sleep,
        client_factory: Callable[[], httpx.Client] | None = None,
        notify: Callable[[], None] = broadcast_update,
        wake: Callable[[], None] = wake_corrections,
        space_recheck_seconds: float = SPACE_RECHECK_SECONDS,
    ) -> None:
        self._root = root
        self._url = url
        self._self_test = self_test
        self._free_bytes = free_bytes
        self._sleep = sleep
        self._client_factory = client_factory or self._default_client
        self._notify = notify
        self._wake = wake
        self._space_recheck_seconds = space_recheck_seconds
        self._notified_at = 0.0
        self._lock = threading.Lock()
        self._thread: threading.Thread | None = None
        self._space_watch: threading.Thread | None = None
        self._state: State = {"state": "idle"}

    @staticmethod
    def _default_client() -> httpx.Client:
        return httpx.Client(
            follow_redirects=True,
            timeout=httpx.Timeout(READ_TIMEOUT_SECONDS, connect=CONNECT_TIMEOUT_SECONDS),
        )

    def status(self) -> State:
        with self._lock:
            return dict(self._state)

    def _set(self, **state: Any) -> None:
        with self._lock:
            changed = state.get("state") != self._state.get("state")
            self._state = state
            now = time.monotonic()
            due = changed or now - self._notified_at >= NOTIFY_MIN_SECONDS
            if due:
                self._notified_at = now
        if due:
            self._notify()

    def start(self, *, wait: bool = False) -> State:
        """Begin the flight, or join the running one. Idempotent; also the retry."""
        with self._lock:
            if self._thread is not None and self._thread.is_alive():
                return dict(self._state)
            if self._state.get("state") == "ready":
                return dict(self._state)
            self._state = {"state": "downloading", "bytes_done": 0, "bytes_total": 0}
            self._request()
            self._thread = threading.Thread(target=self._run, name="classifier-download", daemon=True)
            self._thread.start()
            thread = self._thread
        if wait:
            thread.join()
        return self.status()

    def _requested_marker(self) -> Path:
        root = self._root()
        return root.parent / f".{root.name}.requested"

    def _request(self) -> None:
        """Remember that setup asked for the asset, so a restart before it lands picks the download back up."""
        marker = self._requested_marker()
        try:
            marker.parent.mkdir(parents=True, exist_ok=True)
            marker.touch()
        except OSError:
            logger.warning("Could not record the classifier download request", exc_info=True)

    def resume(self) -> None:
        """At app start: carry on a download that setup asked for and a quit or crash cut short."""
        if self._requested_marker().is_file():
            self.start()

    def _finish(self) -> None:
        self._requested_marker().unlink(missing_ok=True)
        self._set(state="ready")
        self._wake()

    # -- flight ---------------------------------------------------------------

    def _run(self) -> None:
        try:
            self._flight()
        except _Failure as failure:
            logger.warning(
                "Corrections classifier download failed (%s); any installed version keeps serving",
                failure.state.get("code"),
            )
            self._set(**failure.state)
            if failure.state.get("code") == "disk_full":
                self._watch_space()
        except Exception:
            logger.exception("Corrections classifier download failed unexpectedly")
            self._set(
                state="failed", code="unexpected", retryable=True,
                reason="The download couldn’t be set up.",
            )

    def _flight(self) -> None:
        root = self._root()
        pin = PINNED_MANIFESTS[newest_pin()]
        recover_interrupted_install(root)
        if self._already_installed(root):
            self._finish()
            return
        staging = staging_dir(root)
        staging.mkdir(parents=True, exist_ok=True)
        with self._client_factory() as client:
            manifest = self._fetch_manifest(client, staging, pin)
            files = {name: dict(details) for name, details in manifest.files.items()}
            total = sum(int(details["bytes"]) for details in files.values())
            # A damaged file is fetched once more by itself, still reading as an ordinary download.
            for attempt in (1, 2):
                self._check_disk(root, staging, files)
                self._set(state="downloading", bytes_done=self._staged_bytes(staging, files), bytes_total=total)
                for name, details in files.items():
                    self._fetch_file(client, staging / name, name, details, files, total)
                self._set(state="verifying")
                if not self._discard_damaged(staging, files):
                    break
                if attempt == 2:
                    raise _Failure("checksum_failed", "The download was damaged.")
        self._install(root, staging)
        self._finish()

    @staticmethod
    def _discard_damaged(staging: Path, files: dict[str, dict]) -> bool:
        """Delete each staged file that misses its pinned size or sha256; true when any did."""
        damaged = False
        for name, details in files.items():
            path = staging / name
            if path.stat().st_size != int(details["bytes"]) or sha256(path) != details["sha256"]:
                path.unlink(missing_ok=True)
                damaged = True
        return damaged

    def _already_installed(self, root: Path) -> bool:
        """A present asset that passes the runtime's own proof and is the newest pin needs no download.

        An older version that still passes is left serving; the flight goes on to replace it.
        """
        if not (root / "manifest.json").is_file():
            return False
        self._set(state="self_testing")
        previous = previous_dir(root)
        try:
            self._self_test(root)
        except AssetError as error:
            logger.info("Installed corrections classifier did not pass its check: %s", error)
            if not previous.is_dir():
                return False
            # A swap died before its self-test: the parked version is the one that served.
            shutil.rmtree(root, ignore_errors=True)
            os.replace(previous, root)
            try:
                self._self_test(root)
            except AssetError:
                return False
            return root_is_newest_pin(root)
        shutil.rmtree(previous, ignore_errors=True)
        return root_is_newest_pin(root)

    def _fetch_manifest(self, client: httpx.Client, staging: Path, pin: str) -> AssetManifest:
        path = staging / "manifest.json"
        path.unlink(missing_ok=True)
        self._download(client, f"{self._url()}/manifest.json", path)
        if sha256(path) != pin:
            path.unlink(missing_ok=True)
            raise _Failure(
                "manifest_mismatch",
                "The download is not the version this app expects.",
            )
        try:
            return load_manifest(path, CORRECTIONS_SPEC, expected_sha256=pin)
        except AssetError as error:
            path.unlink(missing_ok=True)
            raise _Failure("manifest_mismatch", _reader(error)) from error

    def _staged_bytes(self, staging: Path, files: dict[str, dict]) -> int:
        total = 0
        for name, details in files.items():
            path = staging / name
            if path.is_file():
                total += min(path.stat().st_size, int(details["bytes"]))
        return total

    def _check_disk(self, root: Path, staging: Path, files: dict[str, dict]) -> None:
        needed = sum(int(d["bytes"]) for d in files.values()) - self._staged_bytes(staging, files)
        needed += DISK_MARGIN_BYTES
        free = self._free_bytes(root)
        if free < needed:
            raise _Failure("disk_full", short_of_space(needed - free), bytes_needed=needed)

    def recheck_space(self) -> State:
        """Resume a download that stopped for lack of space once there is room; otherwise refresh how much is missing."""
        state = self.status()
        if state.get("code") != "disk_full":
            return state
        short = int(state["bytes_needed"]) - self._free_bytes(self._root())
        if short <= 0:
            return self.start()
        if state.get("reason") != short_of_space(short):
            self._set(**{**state, "reason": short_of_space(short)})
        return self.status()

    def _watch_space(self) -> None:
        """While stopped for space, look again every minute, whether or not any screen is open."""
        with self._lock:
            if self._space_watch is not None:
                return
            self._space_watch = threading.Thread(target=self._space_watch_loop, name="classifier-space", daemon=True)
            self._space_watch.start()

    def _space_watch_loop(self) -> None:
        while True:
            time.sleep(self._space_recheck_seconds)
            self.recheck_space()
            with self._lock:
                if self._state.get("code") != "disk_full":
                    self._space_watch = None
                    return

    def _fetch_file(
        self, client: httpx.Client, path: Path, name: str, details: dict, files: dict[str, dict], total: int,
    ) -> None:
        size = int(details["bytes"])
        if path.is_file() and path.stat().st_size > size:
            path.unlink()
        if path.is_file() and path.stat().st_size == size:
            return
        base_done = self._staged_bytes(path.parent, {n: d for n, d in files.items() if n != name})

        def progress(done: int) -> None:
            self._set(state="downloading", bytes_done=base_done + done, bytes_total=total)

        self._download(client, f"{self._url()}/{name}", path, expected=size, progress=progress)

    def _download(
        self, client: httpx.Client, url: str, path: Path, *,
        expected: int | None = None, progress: Callable[[int], None] | None = None,
    ) -> None:
        """Stream `url` into `path`, resuming with Range, retrying on network loss."""
        attempt = 0
        while True:
            have = path.stat().st_size if path.is_file() else 0
            try:
                if expected is not None and have == expected:
                    return
                headers = {"Range": f"bytes={have}-"} if have else {}
                with client.stream("GET", url, headers=headers) as response:
                    if response.status_code == 416:
                        path.unlink(missing_ok=True)
                        continue
                    if response.status_code >= 500 or response.status_code == 429:
                        raise httpx.TransportError(f"server busy ({response.status_code})")
                    if response.status_code not in (200, 206):
                        raise _Failure(
                            "download_failed",
                            "The download couldn’t be completed "
                            f"(the server answered {response.status_code}).",
                        )
                    resumed = response.status_code == 206 and have > 0
                    with path.open("ab" if resumed else "wb") as handle:
                        done = have if resumed else 0
                        for chunk in response.iter_raw():
                            handle.write(chunk)
                            done += len(chunk)
                            if progress:
                                progress(done)
                if expected is not None and path.stat().st_size < expected:
                    raise httpx.ReadError("short body")
                return
            except (httpx.TransportError, httpx.StreamError) as error:
                attempt += 1
                if attempt >= RETRY_ATTEMPTS:
                    raise _Failure(
                        "network_lost",
                        "The download was interrupted and couldn’t continue. Check your connection.",
                    ) from error
                self._set(
                    state="downloading", bytes_done=(self.status().get("bytes_done", 0)),
                    bytes_total=self.status().get("bytes_total", 0), interrupted=True,
                )
                self._sleep(RETRY_BACKOFF_SECONDS[min(attempt - 1, len(RETRY_BACKOFF_SECONDS) - 1)])

    def _install(self, root: Path, staging: Path) -> None:
        """Swap the verified folder in by rename, self-test it, then drop the old one.

        The previous root is parked under `.previous` first and restored on any
        failure, so the old version serves until the new one has passed its test.
        """
        previous = previous_dir(root)
        shutil.rmtree(previous, ignore_errors=True)
        root.parent.mkdir(parents=True, exist_ok=True)
        parked = placed = False
        try:
            if root.exists():
                os.replace(root, previous)
                parked = True
            os.replace(staging, root)
            placed = True
            self._set(state="self_testing")
            self._self_test(root)
        except Exception as error:
            damaged = isinstance(error, AssetError)
            if placed and damaged:
                shutil.rmtree(root, ignore_errors=True)
            elif placed:
                os.replace(root, staging)
            if parked:
                os.replace(previous, root)
            if damaged:
                raise _Failure("self_test_failed", _reader(error)) from error
            raise _Failure("install_failed", "The download couldn’t be put in place.") from error
        shutil.rmtree(previous, ignore_errors=True)


FLIGHT = ClassifierDownload()

__all__ = ["ClassifierDownload", "FLIGHT", "base_url", "public_state", "recover_interrupted_install"]
