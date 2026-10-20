"""Immutable raw-CDN retrieval and search helpers for library skill items."""

from __future__ import annotations

import hashlib
import json
from pathlib import PurePosixPath
import re
from typing import Any, Callable
from urllib.parse import quote
from urllib.request import urlopen


class LibraryItemError(RuntimeError):
    """Base error for a requested library item."""


class LibraryIntegrityError(LibraryItemError):
    """An immutable item file did not match its declared digest."""


class LibraryItemUnavailableError(LibraryItemError):
    """A pinned raw item file could not be retrieved."""


REPOSITORY = re.compile(r"^[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+$")
COMMIT = re.compile(r"^[0-9a-fA-F]{40}$")


def source_ref(item: dict[str, Any]) -> str:
    """The stable human-readable source reference for an indexed item."""
    source = item["source"]
    return f"{source['repository']}@{source['commit']}:{source['path']}"


def item_manifest(item: dict[str, Any]) -> str:
    """Digest every declared item field that changes the installed skill experience."""
    declared = {
        "description": item["description"],
        "license": item["license"],
        "source": item["source"],
        "version": item["version"],
        "files": sorted(
            ({"path": row["path"], "sha256": row["sha256"]} for row in item["files"]),
            key=lambda row: row["path"],
        ),
        "withdrawn": item["withdrawn"],
        "withdrawn_reason": item["withdrawn_reason"],
    }
    encoded = json.dumps(declared, sort_keys=True, separators=(",", ":")).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def find_item(skills: tuple[dict[str, Any], ...], name: str) -> dict[str, Any] | None:
    """Return the exact indexed skill name, never a fuzzy install target."""
    return next((item for item in skills if item["name"] == name), None)


def search_items(skills: tuple[dict[str, Any], ...], query: str, *, limit: int = 10) -> list[dict[str, Any]]:
    """Rank non-withdrawn items by whole-word query matches without a list scan elsewhere."""
    words = tuple(token for token in re.findall(r"[a-z0-9]+", query.casefold()) if token)
    if not words:
        return []
    ranked: list[tuple[int, str, dict[str, Any]]] = []
    for item in skills:
        if item["withdrawn"]:
            continue
        name = item["name"].casefold()
        description = item["description"].casefold()
        score = sum(4 * (word in name) + (word in description) for word in words)
        if score:
            ranked.append((-score, item["name"], item))
    return [item for _score, _name, item in sorted(ranked)[:limit]]


def fetch_bundle(
    item: dict[str, Any], *, fetch: Callable[[str], bytes] | None = None,
) -> dict[str, bytes]:
    """Fetch every item file from its immutable raw commit and verify before writing."""
    source = item["source"]
    repository, commit, base = source["repository"], source["commit"], source["path"]
    if not REPOSITORY.fullmatch(repository) or not COMMIT.fullmatch(commit):
        raise LibraryItemError("invalid library item source")
    validate_relative_path(base)
    getter = fetch or _fetch_raw
    bundle: dict[str, bytes] = {}
    for file_row in item["files"]:
        relative = file_row["path"]
        validate_relative_path(relative)
        url = _raw_url(repository, commit, base, relative)
        try:
            data = getter(url)
        except (OSError, TimeoutError) as error:
            raise LibraryItemUnavailableError(f"could not retrieve {relative} from its reviewed commit") from error
        expected = file_row["sha256"].removeprefix("sha256:").casefold()
        if hashlib.sha256(data).hexdigest() != expected:
            raise LibraryIntegrityError(f"integrity check failed for {relative}")
        bundle[relative] = data
    return bundle


def _raw_url(repository: str, commit: str, base: str, relative: str) -> str:
    """Build the only item fetch route: raw CDN at an immutable commit."""
    segments = [segment for value in (base, relative) for segment in value.split("/")]
    path = "/".join(quote(segment, safe="._-") for segment in segments)
    return f"https://raw.githubusercontent.com/{repository}/{commit}/{path}"


def _fetch_raw(url: str) -> bytes:
    with urlopen(url, timeout=10) as response:  # nosec B310 - fixed raw-GitHub origin and validated segments
        return response.read()


def validate_relative_path(value: str) -> None:
    parsed = PurePosixPath(value)
    if not value or value.startswith("/") or "\\" in value or any(part in ("", ".", "..") for part in parsed.parts):
        raise LibraryItemError("invalid library item path")
