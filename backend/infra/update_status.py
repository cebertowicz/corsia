"""Update availability for a source checkout or packaged app.

Source checkouts fetch the current branch's remote ref and compare it with
``HEAD``. Packaged apps read their build-stamped Tauri updater manifest and
require both a newer version and an entry for this machine's Tauri platform.

The result is cached process-wide with a TTL because either path makes a
network request. Poll frequency in the browser therefore does not determine how
often the remote is contacted, and concurrent cache misses share one probe.

An unreachable remote, a detached HEAD, or any invalid update metadata yields
``available: None`` (unknown) — never a confident "up to date", which would be a
false negative that hides a real update.
"""

from __future__ import annotations

import json
import os
import platform
import subprocess
import threading
import time
from dataclasses import dataclass, asdict
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.request import urlopen

import semver

import infra.build_info as build_info
from infra import platform_paths

# Serving checkout root: backend/infra/update_status.py -> repo root.
_SERVING_ROOT = Path(__file__).resolve().parent.parent.parent

# The remote is contacted at most once per this window, regardless of poll rate.
CACHE_TTL_SECONDS = 300
_PACKAGE_TIMEOUT_SECONDS = 5
_UPDATER_ENDPOINT_RESOURCE = "updater-endpoint.json"

_cache: dict = {"data": None, "ts": 0.0}
_cache_condition = threading.Condition()
_probe_in_flight = False


@dataclass(frozen=True)
class UpdateStatus:
    """What the UI needs to decide whether to offer an update."""

    branch: str | None
    current_sha: str | None
    remote_sha: str | None
    commits_behind: int | None
    available: bool | None
    checked_at: float | None
    error: str | None
    current_version: str | None
    target_version: str | None
    changes: dict | None

    def to_dict(self) -> dict:
        return asdict(self)


def _git(args: list[str], timeout: int = 30) -> str:
    """Run one git command in the serving checkout; raise on failure."""
    result = subprocess.run(
        ["git", *args],
        cwd=_SERVING_ROOT,
        capture_output=True,
        text=True,
        timeout=timeout,
        check=False,
    )
    if result.returncode != 0:
        raise RuntimeError((result.stderr or result.stdout).strip()[:200])
    return result.stdout.strip()


def _unknown(error: str) -> UpdateStatus:
    """Unknown state — the UI must not claim the instance is up to date."""
    return UpdateStatus(
        branch=None,
        current_sha=None,
        remote_sha=None,
        commits_behind=None,
        available=None,
        checked_at=time.time(),
        error=error,
        current_version=None,
        target_version=None,
        changes=None,
    )


def _bundle_endpoint(bundle: Path) -> str:
    """Read the build-resolved updater endpoint from the signed app resources."""
    resource = bundle / "Contents" / "Resources" / _UPDATER_ENDPOINT_RESOURCE
    try:
        payload = json.loads(resource.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise RuntimeError(f"Could not read bundled updater endpoint: {exc}") from exc
    endpoint = payload.get("endpoint") if isinstance(payload, dict) else None
    if (
        not isinstance(endpoint, str)
        or not endpoint.startswith("https:" + "//")
        or any(ch.isspace() for ch in endpoint)
    ):
        raise RuntimeError("Bundled updater endpoint is invalid.")
    return endpoint


def normalize_packaged_version(version: str) -> str:
    """Return the Tauri manifest's canonical semantic version form."""
    return str(semver.Version.parse(version))


def _bundle_current_version() -> str:
    """Return the running bundle version from the canonical build-info owner."""
    version = build_info.app_version()
    if not isinstance(version, str):
        raise RuntimeError("Running bundle version is unavailable.")
    return normalize_packaged_version(version)


def _manifest(endpoint: str) -> dict:
    """Fetch and decode the Tauri updater manifest with a bounded timeout."""
    try:
        with urlopen(endpoint, timeout=_PACKAGE_TIMEOUT_SECONDS) as response:  # noqa: S310 -- endpoint is build-stamped
            payload = json.loads(response.read().decode("utf-8"))
    except (HTTPError, URLError, OSError, TimeoutError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise RuntimeError(f"Could not fetch packaged update manifest: {exc}") from exc
    if not isinstance(payload, dict):
        raise RuntimeError("Packaged update manifest must be a JSON object.")
    return payload


def _manifest_changes(notes: object) -> dict | None:
    """Turn Tauri's multiline release notes into the existing changes contract."""
    if notes is None:
        return None
    if not isinstance(notes, str):
        raise RuntimeError("Packaged update manifest notes must be text.")
    items = []
    for line in notes.splitlines():
        item = line.strip()
        if item.startswith(("- ", "* ")):
            item = item[2:].strip()
        if item:
            items.append(item)
    if not items:
        return None
    return {
        "kind": "changelog",
        "headline": None,
        "items": items[:50],
        "more": max(0, len(items) - 50),
    }


def _tauri_platform_key() -> str:
    """Return Tauri's updater manifest key for this machine."""
    operating_system = platform.system().lower()
    architecture = platform.machine().lower()
    if operating_system == "darwin" and architecture == "arm64":
        architecture = "aarch64"
    return f"{operating_system}-{architecture}"


def _packaged_probe(bundle: Path) -> UpdateStatus:
    """Compare the running app's stamped version with its build-stamped feed."""
    try:
        current_version = _bundle_current_version()
        manifest = _manifest(_bundle_endpoint(bundle))
        target_version = manifest.get("version")
        if not isinstance(target_version, str):
            raise RuntimeError("Packaged update manifest has no version.")
        target_version = normalize_packaged_version(target_version)
        current = semver.Version.parse(current_version)
        target = semver.Version.parse(target_version)
        platforms = manifest.get("platforms")
        if not isinstance(platforms, dict):
            raise RuntimeError("Packaged update manifest has no platforms.")
        has_current_platform = _tauri_platform_key() in platforms
        changes = _manifest_changes(manifest.get("notes"))
    except (RuntimeError, ValueError, TypeError) as exc:
        return _unknown(str(exc))
    return UpdateStatus(
        branch=None,
        current_sha=None,
        remote_sha=None,
        commits_behind=None,
        available=target > current and has_current_platform,
        checked_at=time.time(),
        error=None,
        current_version=str(current),
        target_version=str(target),
        changes=changes,
    )


def _probe() -> UpdateStatus:
    """Probe git for source checkouts or the Tauri manifest for packaged apps."""
    if os.environ.get("CORSIA_TEST_MODE") == "1":
        return _unknown("Update checks are disabled in test mode.")
    bundle = platform_paths.app_bundle_root()
    if bundle is not None:
        return _packaged_probe(bundle)
    try:
        branch = _git(["symbolic-ref", "--quiet", "--short", "HEAD"])
    except (RuntimeError, OSError, subprocess.SubprocessError):
        return _unknown("Serving checkout is in detached HEAD; no branch to check.")

    try:
        current = _git(["rev-parse", "HEAD"])
        _git(["fetch", "--quiet", "origin", branch])
        remote = _git(["rev-parse", f"origin/{branch}"])
    except subprocess.TimeoutExpired:
        return _unknown("Update check timed out contacting the remote.")
    except (RuntimeError, OSError, subprocess.SubprocessError) as exc:
        return _unknown(f"Could not reach the remote: {exc}")

    # Only commits the remote has and we do not constitute an update. A branch
    # that is merely *ahead* (local commits not yet pushed) differs from its
    # remote SHA but has nothing to pull, so SHA inequality is not the signal.
    try:
        counts = _git(["rev-list", "--left-right", "--count", f"HEAD...origin/{branch}"])
        behind = int(counts.split()[1])
    except (RuntimeError, OSError, ValueError, IndexError, subprocess.SubprocessError):
        # Without a reliable count we cannot tell "ahead" from "behind"; report
        # unknown rather than guessing an update in either direction.
        return UpdateStatus(
            branch=branch,
            current_sha=current,
            remote_sha=remote,
            commits_behind=None,
            available=None,
            checked_at=time.time(),
            error="Could not determine how far behind the remote this checkout is.",
            current_version=None,
            target_version=None,
            changes=None,
        )

    available = behind > 0
    from infra.update_changes import build_update_details

    details = build_update_details(available, branch, _git)
    return UpdateStatus(
        branch=branch,
        current_sha=current,
        remote_sha=remote,
        commits_behind=behind,
        available=available,
        checked_at=time.time(),
        error=None,
        current_version=details.current_version,
        target_version=details.target_version,
        changes=details.changes,
    )


def update_status(force: bool = False) -> UpdateStatus:
    """Return cached status, coalescing simultaneous refreshes into one probe."""
    global _probe_in_flight
    with _cache_condition:
        now = time.monotonic()
        cached = _cache["data"]
        if not force and cached is not None and now - _cache["ts"] < CACHE_TTL_SECONDS:
            return cached
        waited_for_probe = False
        while _probe_in_flight:
            waited_for_probe = True
            _cache_condition.wait()
        cached = _cache["data"]
        if waited_for_probe and cached is not None:
            return cached
        _probe_in_flight = True

    try:
        status = _probe()
    except BaseException:
        with _cache_condition:
            _probe_in_flight = False
            _cache_condition.notify_all()
        raise

    with _cache_condition:
        _cache["data"] = status
        _cache["ts"] = time.monotonic()
        _probe_in_flight = False
        _cache_condition.notify_all()
    return status


def cached_target_version() -> str | None:
    """Return the cached target version without probing the remote."""
    cached = _cache["data"]
    return cached.target_version if cached is not None else None


def cached_changes() -> dict | None:
    """Return the change list the cached probe offered, without probing the remote."""
    cached = _cache["data"]
    return cached.changes if cached is not None else None


def invalidate_cache() -> None:
    """Drop the cached answer so the next read re-probes the remote."""
    with _cache_condition:
        _cache["data"] = None
        _cache["ts"] = 0.0
