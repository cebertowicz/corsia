"""Synchronous, cache-backed reader for the public CORSIA library index."""

from __future__ import annotations

from dataclasses import dataclass
import json
import math
import os
from pathlib import Path
import re
import time
from typing import Any
from urllib.request import urlopen

from library.items import COMMIT, REPOSITORY, LibraryItemError, validate_relative_path

INDEX_URL = "https://raw.githubusercontent.com/cebertowicz/corsia-library/main/index.json"
_CACHE_NAME = "index.json"
_SHA256 = re.compile(r"^(?:sha256:)?[0-9a-fA-F]{64}$")


class LibraryIndexError(RuntimeError):
    """Base error for an unusable library index."""


class LibraryUnreachableError(LibraryIndexError):
    """Raised when neither the raw library index nor a valid cache is available."""


class LibraryIndexValidationError(LibraryIndexError):
    """Raised when an index does not have the minimum safe shape."""


@dataclass(frozen=True)
class LibraryIndex:
    """Validated library rows and the age of the source used to read them."""

    skills: tuple[dict[str, Any], ...]
    index_age: str
    refreshed: bool


def load_index() -> LibraryIndex:
    """Fetch the index, falling back to its last valid cached copy when offline."""
    now = time.time()
    cached = _read_cache()
    try:
        payload = _validate_index(_fetch_index())
    except LibraryIndexValidationError:
        # A reachable but malformed index is a publisher error, not an offline
        # condition. Keeping it visible avoids silently serving an older catalog.
        raise
    except (OSError, TimeoutError, UnicodeDecodeError, json.JSONDecodeError, ValueError) as error:
        if cached is None:
            raise LibraryUnreachableError(
                "library_unreachable: could not fetch the library index and no valid cached index exists"
            ) from error
        payload, fetched_at = cached
        return LibraryIndex(payload, _format_age(now - fetched_at), refreshed=False)

    refreshed = cached is None or payload != cached[0]
    _write_cache(payload, now)
    return LibraryIndex(payload, "just now", refreshed=refreshed)


def _fetch_index() -> Any:
    with urlopen(INDEX_URL, timeout=10) as response:  # nosec B310 - fixed public URL
        return json.loads(response.read().decode("utf-8"))


def _state_root() -> Path:
    from infra.config import STATE_ROOT

    return Path(STATE_ROOT)


def _cache_path() -> Path:
    return _state_root() / "library" / _CACHE_NAME


def _read_cache() -> tuple[tuple[dict[str, Any], ...], float] | None:
    try:
        cached = json.loads(_cache_path().read_text(encoding="utf-8"))
        if not isinstance(cached, dict):
            return None
        fetched_at = cached.get("fetched_at")
        if isinstance(fetched_at, bool) or not isinstance(fetched_at, (int, float)):
            return None
        if not math.isfinite(fetched_at):
            return None
        return _validate_index(cached.get("index")), float(fetched_at)
    except (OSError, ValueError, TypeError, LibraryIndexValidationError):
        return None


def _write_cache(payload: tuple[dict[str, Any], ...], fetched_at: float) -> None:
    target = _cache_path()
    target.parent.mkdir(parents=True, exist_ok=True)
    staged = target.with_name(f".{target.name}.{os.getpid()}.tmp")
    try:
        with staged.open("w", encoding="utf-8") as handle:
            json.dump({"fetched_at": fetched_at, "index": {"skills": payload}}, handle)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(staged, target)
    finally:
        try:
            staged.unlink()
        except FileNotFoundError:
            pass


def _validate_index(payload: Any) -> tuple[dict[str, Any], ...]:
    if not isinstance(payload, dict) or not isinstance(payload.get("skills"), list):
        raise LibraryIndexValidationError("library index must contain a top-level skills list")
    skills = tuple(_validate_skill(row) for row in payload["skills"])
    names = [skill["name"].casefold() for skill in skills]
    if len(names) != len(set(names)):
        raise LibraryIndexValidationError("library index skill names must be unique, ignoring case")
    return skills


def _validate_skill(row: Any) -> dict[str, Any]:
    required = (
        "name", "description", "version", "license", "source", "files", "withdrawn", "withdrawn_reason",
    )
    if not isinstance(row, dict) or any(field not in row for field in required):
        raise LibraryIndexValidationError("library index skill row is missing a required field")
    for field in ("name", "description", "version", "license"):
        if not isinstance(row[field], str) or not row[field]:
            raise LibraryIndexValidationError(f"library index skill {field} must be a non-empty string")
    if not isinstance(row["withdrawn"], bool):
        raise LibraryIndexValidationError("library index skill withdrawn must be a boolean")
    if row["withdrawn_reason"] is not None and not isinstance(row["withdrawn_reason"], str):
        raise LibraryIndexValidationError("library index skill withdrawn_reason must be a string or null")
    if row["withdrawn"] and not row["withdrawn_reason"]:
        raise LibraryIndexValidationError("withdrawn library skills must include withdrawn_reason")

    source = row["source"]
    if not isinstance(source, dict):
        raise LibraryIndexValidationError("library index skill source must be an object")
    for field in ("repository", "path", "commit"):
        if not isinstance(source.get(field), str) or not source[field]:
            raise LibraryIndexValidationError(f"library index source {field} must be a non-empty string")
    if not REPOSITORY.fullmatch(source["repository"]):
        raise LibraryIndexValidationError("library index source repository must be an owner/name pair")
    if not COMMIT.fullmatch(source["commit"]):
        raise LibraryIndexValidationError("library index source commit must be a full 40-character SHA")
    _validate_relative_path(source["path"])

    files = row["files"]
    if not isinstance(files, list):
        raise LibraryIndexValidationError("library index skill files must be a list")
    paths: list[str] = []
    for file_row in files:
        if not isinstance(file_row, dict):
            raise LibraryIndexValidationError("library index file must be an object")
        path, sha256 = file_row.get("path"), file_row.get("sha256")
        if not isinstance(path, str):
            raise LibraryIndexValidationError("library index file path must be a string")
        _validate_relative_path(path)
        paths.append(path)
        if not isinstance(sha256, str) or not _SHA256.fullmatch(sha256):
            raise LibraryIndexValidationError(
                "library index file sha256 must be 64 hexadecimal characters, optionally prefixed sha256:"
            )
    if "SKILL.md" not in paths:
        raise LibraryIndexValidationError("library index skill files must include SKILL.md")
    if len(paths) != len({path.casefold() for path in paths}):
        raise LibraryIndexValidationError("library index file paths must be unique, ignoring case")
    return dict(row)


def _validate_relative_path(path: str) -> None:
    try:
        validate_relative_path(path)
    except LibraryItemError as error:
        raise LibraryIndexValidationError(f"library index contains an unsafe relative path: {path!r}") from error


def _format_age(seconds: float) -> str:
    seconds = max(0, seconds)
    if seconds < 60:
        return "just now"
    minutes = int(seconds // 60)
    if minutes < 60:
        return f"{minutes} minute{'s' if minutes != 1 else ''} ago"
    hours = minutes // 60
    if hours < 24:
        return f"{hours} hour{'s' if hours != 1 else ''} ago"
    days = hours // 24
    return f"{days} day{'s' if days != 1 else ''} ago"
