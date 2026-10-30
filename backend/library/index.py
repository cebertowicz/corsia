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

import semver

from library.items import COMMIT, LIBRARY_REPOSITORY, REPOSITORY, LibraryItemError, validate_relative_path
from library.pack_validation import validate_blueprint_path

INDEX_URL = f"https://raw.githubusercontent.com/{LIBRARY_REPOSITORY}/main/index.json"
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
    """Validated library rows and the age and per-item generation of the source used to read them."""

    items: tuple[dict[str, Any], ...]
    index_age: str
    refreshed: bool
    generations: dict[str, int]


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
        payload, fetched_at, generations = cached
        return LibraryIndex(payload, _format_age(now - fetched_at), refreshed=False, generations=generations)

    prior_items = {} if cached is None else {item["name"]: item for item in cached[0]}
    prior_generations = {} if cached is None else cached[2]
    generations = {
        item["name"]: max(1, prior_generations.get(item["name"], 0) + int(prior_items.get(item["name"]) != item))
        for item in payload
    }
    refreshed = cached is None or payload != cached[0]
    _write_cache(payload, now, generations)
    return LibraryIndex(payload, "just now", refreshed=refreshed, generations=generations)


def _fetch_index() -> Any:
    with urlopen(INDEX_URL, timeout=10) as response:  # nosec B310 - fixed public URL
        return json.loads(response.read().decode("utf-8"))


def _state_root() -> Path:
    from infra.config import STATE_ROOT

    return Path(STATE_ROOT)


def _cache_path() -> Path:
    return _state_root() / "library" / _CACHE_NAME


def _read_cache() -> tuple[tuple[dict[str, Any], ...], float, dict[str, int]] | None:
    try:
        cached = json.loads(_cache_path().read_text(encoding="utf-8"))
        if not isinstance(cached, dict):
            return None
        fetched_at = cached.get("fetched_at")
        if isinstance(fetched_at, bool) or not isinstance(fetched_at, (int, float)):
            return None
        if not math.isfinite(fetched_at):
            return None
        generations = cached.get("generations", {})
        if not isinstance(generations, dict) or any(
            not isinstance(name, str) or isinstance(generation, bool) or not isinstance(generation, int) or generation < 1
            for name, generation in generations.items()
        ):
            return None
        return _validate_index(cached.get("index")), float(fetched_at), generations
    except (OSError, ValueError, TypeError, LibraryIndexValidationError):
        return None


def _write_cache(payload: tuple[dict[str, Any], ...], fetched_at: float, generations: dict[str, int]) -> None:
    target = _cache_path()
    target.parent.mkdir(parents=True, exist_ok=True)
    staged = target.with_name(f".{target.name}.{os.getpid()}.tmp")
    try:
        with staged.open("w", encoding="utf-8") as handle:
            rows = [
                {**item, "source": "library"}
                if item["source"]["repository"] == LIBRARY_REPOSITORY and item["source"]["commit"] == "main"
                else item for item in payload
            ]
            json.dump({"fetched_at": fetched_at, "generations": generations, "index": {"skills": rows}}, handle)
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
    items = tuple(_validate_item(row) for row in payload["skills"] if not _unsupported_item(row))
    names = [item["name"].casefold() for item in items]
    if len(names) != len(set(names)):
        raise LibraryIndexValidationError("library index item names must be unique, ignoring case")
    return items


def _unsupported_item(row: Any) -> bool:
    if not isinstance(row, dict):
        return False
    kind = row.get("kind", "skill")
    return isinstance(kind, str) and (kind not in {"skill", "pack"} or (kind == "pack" and row.get("pack_kind") != "blueprint"))


def _validate_item(row: Any) -> dict[str, Any]:
    if not isinstance(row, dict):
        raise LibraryIndexValidationError("library index item must be an object")
    kind = row.get("kind", "skill")
    if not isinstance(kind, str):
        raise LibraryIndexValidationError("library index item kind must be skill or pack")
    if kind == "pack":
        if row.get("type") != "corsia-blueprint":
            raise LibraryIndexValidationError("library blueprint type must be corsia-blueprint")
        if not isinstance(row.get("digest"), str) or not _SHA256.fullmatch(row["digest"]):
            raise LibraryIndexValidationError("library blueprint digest must be SHA-256")
    required = (
        "name", "description", "version", "source", "files", "withdrawn", "withdrawn_reason",
    ) + (("license",) if kind == "skill" else ())
    if any(field not in row for field in required):
        raise LibraryIndexValidationError("library index item row is missing a required field")
    for field in ("name", "description", "version") + (("license",) if kind == "skill" else ()):
        if not isinstance(row[field], str) or not row[field]:
            raise LibraryIndexValidationError(f"library index item {field} must be a non-empty string")
    if kind == "pack":
        if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_-]*", row["name"]):
            raise LibraryIndexValidationError("library blueprint name must be a safe single path component")
        try:
            semver.Version.parse(row["version"])
        except ValueError as error:
            raise LibraryIndexValidationError("library blueprint version must be semver") from error
    if not isinstance(row["withdrawn"], bool):
        raise LibraryIndexValidationError("library index item withdrawn must be a boolean")
    if row["withdrawn_reason"] is not None and not isinstance(row["withdrawn_reason"], str):
        raise LibraryIndexValidationError("library index item withdrawn_reason must be a string or null")
    if row["withdrawn"] and not row["withdrawn_reason"]:
        raise LibraryIndexValidationError("withdrawn library items must include withdrawn_reason")

    source = row["source"]
    own_item = source == "library"
    if own_item:
        url = row.get("url")
        pattern = r"skills/[^/]+/SKILL\.md" if kind == "skill" else r"packs/[^/]+/"
        if not isinstance(url, str) or not re.fullmatch(pattern, url) or url.split("/")[1] in {".", ".."}:
            raise LibraryIndexValidationError("library-owned item url must identify its item folder")
        base_path = url.removesuffix("/SKILL.md") if kind == "skill" else url.removesuffix("/")
        _validate_relative_path(base_path)
        source = {"repository": LIBRARY_REPOSITORY, "path": base_path, "commit": "main"}
    if not isinstance(source, dict):
        raise LibraryIndexValidationError("library index item source must be an object")
    for field in ("repository", "path", "commit"):
        if not isinstance(source.get(field), str) or not source[field]:
            raise LibraryIndexValidationError(f"library index source {field} must be a non-empty string")
    if not REPOSITORY.fullmatch(source["repository"]):
        raise LibraryIndexValidationError("library index source repository must be an owner/name pair")
    if not own_item and not COMMIT.fullmatch(source["commit"]):
        raise LibraryIndexValidationError("library index source commit must be a full 40-character SHA")
    _validate_relative_path(source["path"])

    files = row["files"]
    if not isinstance(files, list):
        raise LibraryIndexValidationError("library index item files must be a list")
    paths: list[str] = []
    for file_row in files:
        if not isinstance(file_row, dict):
            raise LibraryIndexValidationError("library index file must be an object")
        path, sha256 = file_row.get("path"), file_row.get("sha256")
        if not isinstance(path, str):
            raise LibraryIndexValidationError("library index file path must be a string")
        if kind == "pack":
            try:
                validate_blueprint_path(path)
            except LibraryItemError as error:
                raise LibraryIndexValidationError(f"library blueprint contains an unsafe path: {path!r}") from error
        else:
            _validate_relative_path(path)
        paths.append(path)
        if not isinstance(sha256, str) or not _SHA256.fullmatch(sha256):
            raise LibraryIndexValidationError(
                "library index file sha256 must be 64 hexadecimal characters, optionally prefixed sha256:"
            )
    if kind == "skill" and "SKILL.md" not in paths:
        raise LibraryIndexValidationError("library index skill files must include SKILL.md")
    if kind == "pack" and not {"manifest.yaml", "CHANGELOG.md"}.issubset(paths):
        raise LibraryIndexValidationError("library blueprint must include manifest.yaml and CHANGELOG.md")
    if len(paths) != len({path.casefold() for path in paths}):
        raise LibraryIndexValidationError("library index file paths must be unique, ignoring case")
    return {**row, "source": source}


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
