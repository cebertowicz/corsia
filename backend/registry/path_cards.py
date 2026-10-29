"""Reference cards: the registered artifacts a message's text links to or names.

The server half of ``frontend/src/lib/linkTargetResolver.ts``. The client turns a
clicked link into a path and opens the artifact; here the same turning runs over
a message's text so a card can stand for each file the message names. The path
rules (``link_target_path``, ``normalize_path``, ``workspace_path``) are ports of
the client's, held to each other by ``testdata/link-target-vectors.json``, which
both test suites read.

An accessible registered artifact or existing scoped file becomes a card.
"""

from __future__ import annotations

import re
from collections.abc import Collection, Mapping, Sequence
from pathlib import Path
from urllib.parse import unquote

from registry.chat_cards import card_for, reference_card
from registry import service as registry_service
from registry.path_access import FileLookup, lookup_file

_FENCE = re.compile(r"^(```|~~~).*?^\1", re.MULTILINE | re.DOTALL)
_MARKDOWN_LINK = re.compile(r"\[[^\]\n]*\]\(\s*<?([^)\s>]+)>?(?:\s+\"[^\"]*\")?\s*\)")
# File paths in prose or inline code; URLs are masked before this scan.
_BARE_PATH = re.compile(r"(?<![\w/.:~%-])((?:/|(?:\.{1,2}/))?(?:[\w@%+=,.~-]+/)*[\w@%+=,~-][\w@%+=,.~-]*\.[A-Za-z0-9]{1,8})(?![\w/])")
_URL = re.compile(r"[a-z][a-z\d+.-]*://[^\s<>()]+", re.IGNORECASE)
_BAD_ESCAPE = re.compile(r"%(?![0-9a-fA-F]{2})")
_SCHEME = re.compile(r"^[a-z][a-z\d+.-]*:", re.IGNORECASE)


def link_target_path(href: str) -> str | None:
    """The path part of a link target, decoded, or None when it has none or is malformed."""
    without_query = re.split(r"[?#]", href, maxsplit=1)[0]
    if not without_query or _BAD_ESCAPE.search(without_query):
        return None
    try:
        return unquote(without_query, errors="strict")
    except UnicodeDecodeError:
        return None


def normalize_path(root: str, target: str) -> str | None:
    """``target`` resolved under ``root`` without leaving it, or None."""
    root_parts = [part for part in root.split("/") if part]
    parts = [] if target.startswith("/") else list(root_parts)
    for part in target.split("/"):
        if not part or part == ".":
            continue
        if part == "..":
            if len(parts) <= len(root_parts):
                return None
            parts.pop()
            continue
        parts.append(part)
    resolved = "/" + "/".join(parts)
    return resolved if resolved == root or resolved.startswith(root + "/") else None


def workspace_path(href: str, roots: Sequence[str], origin: str | None = None) -> str | None:
    """The scoped path a link names: absolute under a root, or relative to its workspace origin."""
    target = link_target_path(href)
    if not target or not roots:
        return None
    if target.startswith("/"):
        root = next((r for r in roots if target == r or target.startswith(r + "/")), None)
        return normalize_path(root, target) if root else None
    return normalize_path(origin or roots[0], target)


def targets(text: str) -> list[str]:
    """Link targets and bare paths outside fenced code, in the order text names them."""
    body = _FENCE.sub("", text or "")
    found: list[tuple[int, str]] = [(m.start(), m.group(1)) for m in _MARKDOWN_LINK.finditer(body)]
    bare_body = _MARKDOWN_LINK.sub(lambda m: " " * len(m.group()), body)
    bare_body = _URL.sub(lambda m: " " * len(m.group()), bare_body)
    found += [(m.start(), m.group(1)) for m in _BARE_PATH.finditer(bare_body)]
    ordered: list[str] = []
    seen: set[str] = set()
    for _pos, target in sorted(found):
        if not _SCHEME.match(target) and target not in seen:
            seen.add(target)
            ordered.append(target)
    return ordered


def reference_cards(
    db, text: str, bases: Sequence[Path], user_id: str, *, covered: Mapping[str, Collection[str]] | None = None,
    lookup_memo: dict[str, FileLookup] | None = None, origin: Path | None = None,
) -> list[dict]:
    """Cards for accessible files ``text`` names.

    ``covered`` maps an artifact id to the holds whose ask card covers it: the card is
    kept and marked ``covered_by_ask``, so the client shows it only when none of those
    ask cards is on screen. The server never drops the reviewed document.
    """
    roots = [str(base) for base in bases]
    lookups = lookup_memo if lookup_memo is not None else {}
    cards: list[dict] = []
    seen_paths: set[str] = set()
    seen_ids: set[str] = set()
    for target in targets(text):
        path = workspace_path(target, roots, str(origin) if origin else None)
        if path is None:
            continue
        found = lookups.get(path)
        if found is None:
            found = lookup_file(path, list(bases), db=db)
            lookups[path] = found
        if not found.allowed or not found.is_file or found.path is None:
            continue
        canonical = str(found.path)
        artifact_id = str(found.artifact["id"]) if found.artifact else None
        if canonical in seen_paths or (artifact_id and artifact_id in seen_ids):
            continue
        if found.artifact:
            card = reference_card(db, found.artifact, user_id)
        else:
            # An identity without a live owner is not an unregistered file. In particular,
            # a retired file must not reappear as an openable file-only card.
            if found.has_identity:
                continue
            card = card_for({"state": "active", "path": canonical, "type": registry_service.classify(found.path),
                             "integration": "workspace"}, None, kind="artifact")
        if card is not None:
            seen_paths.add(canonical)
            if artifact_id:
                seen_ids.add(artifact_id)
            if covered and artifact_id and covered.get(artifact_id):
                card["covered_by_ask"] = sorted(covered[artifact_id])
            cards.append(card)
    return cards
