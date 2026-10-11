"""Reference cards: the registered artifacts a message's text links to or names.

The server half of ``frontend/src/lib/linkTargetResolver.ts``. The client turns a
clicked link into a path and opens the artifact; here the same turning runs over
a message's text so a card can stand for each file the message names. The path
rules (``link_target_path``, ``normalize_path``, ``workspace_path``) are ports of
the client's, held to each other by ``testdata/link-target-vectors.json``, which
both test suites read.

Only a registered artifact the reading user may open becomes a card, and a plain
file the message happens to mention does not. At most ``MAX_CARDS`` are returned,
with the count of those past the cap, and at most ``MAX_CANDIDATES`` targets are
looked at, so a long message costs a bounded number of lookups.
"""

from __future__ import annotations

import re
from collections.abc import Collection, Mapping, Sequence
from pathlib import Path
from urllib.parse import unquote

from registry.chat_cards import reference_card, reference_cards as live_reference_cards
from registry.path_access import lookup_file, lookup_files

MAX_CARDS = 8
MAX_CANDIDATES = 40

_FENCE = re.compile(r"^(```|~~~).*?^\1", re.MULTILINE | re.DOTALL)
_MARKDOWN_LINK = re.compile(r"\[[^\]\n]*\]\(\s*<?([^)\s>]+)>?(?:\s+\"[^\"]*\")?\s*\)")
# An absolute path with a file extension, as an agent writes it in prose or code.
_BARE_PATH = re.compile(r"(?<![\w/.:~-])(/(?:[\w@%+=,.~-]+/)*[\w@%+=,~-][\w@%+=,.~-]*\.[A-Za-z0-9]{1,8})(?![\w/])")
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


def workspace_path(href: str, roots: Sequence[str]) -> str | None:
    """The workspace path a link names: absolute under a root, or relative to the first."""
    target = link_target_path(href)
    if not target or not roots:
        return None
    if target.startswith("/"):
        root = next((r for r in roots if target == r or target.startswith(r + "/")), None)
        return normalize_path(root, target) if root else None
    return normalize_path(roots[0], target)


def targets(text: str) -> list[str]:
    """Link targets and bare absolute paths outside fenced code, in the order text names them."""
    body = _FENCE.sub("", text or "")
    found: list[tuple[int, str]] = [(m.start(), m.group(1)) for m in _MARKDOWN_LINK.finditer(body)]
    found += [(m.start(), m.group(1)) for m in _BARE_PATH.finditer(body)]
    ordered: list[str] = []
    for _pos, target in sorted(found):
        if not _SCHEME.match(target) and target not in ordered:
            ordered.append(target)
    return ordered[:MAX_CANDIDATES]


def reference_cards_by_message(
    db,
    texts: Mapping[str, str],
    bases: Sequence[Path],
    user_id: str,
    *,
    covered: Mapping[str, Mapping[str, Collection[str]]] | None = None,
) -> dict[str, tuple[list[dict], int]]:
    """Reference cards for a page of message text, resolving files and access once."""
    roots = [str(base) for base in bases]
    paths_by_message: dict[str, list[str]] = {}
    for message_id, text in texts.items():
        paths_by_message[message_id] = [
            path
            for target in targets(text)
            if (path := workspace_path(target, roots)) is not None
        ]
    found = lookup_files(
        [path for paths in paths_by_message.values() for path in paths], list(bases), db=db,
    )
    artifacts = {
        str(lookup.artifact["id"]): lookup.artifact
        for lookup in found.values()
        if lookup.artifact is not None
    }
    cards_by_artifact = live_reference_cards(db, artifacts, user_id)
    result: dict[str, tuple[list[dict], int]] = {}
    for message_id, paths in paths_by_message.items():
        cards: list[dict] = []
        seen: set[str] = set()
        for path in paths:
            artifact = found[path].artifact
            if artifact is None:
                continue
            artifact_id = str(artifact["id"])
            if artifact_id in seen or artifact_id not in cards_by_artifact:
                continue
            seen.add(artifact_id)
            card = dict(cards_by_artifact[artifact_id])
            holds = covered.get(message_id, {}).get(artifact_id) if covered else None
            if holds:
                card["covered_by_ask"] = sorted(holds)
            cards.append(card)
        result[message_id] = (cards[:MAX_CARDS], max(0, len(cards) - MAX_CARDS))
    return result


def reference_cards(
    db, text: str, bases: Sequence[Path], user_id: str, *, covered: Mapping[str, Collection[str]] | None = None,
) -> tuple[list[dict], int]:
    """Cards for the registered artifacts ``text`` names, and how many more past the cap.

    ``covered`` maps an artifact id to the holds whose ask card covers it: the card is
    kept and marked ``covered_by_ask``, so the client shows it only when none of those
    ask cards is on screen. The server never drops the reviewed document.
    """
    roots = [str(base) for base in bases]
    cards: list[dict] = []
    seen: set[str] = set()
    for target in targets(text):
        path = workspace_path(target, roots)
        if path is None:
            continue
        found = lookup_file(path, list(bases), db=db)
        if found.artifact is None or str(found.artifact["id"]) in seen:
            continue
        card = reference_card(db, found.artifact, user_id)
        if card is not None:
            seen.add(card["artifact_id"])
            if covered and covered.get(card["artifact_id"]):
                card["covered_by_ask"] = sorted(covered[card["artifact_id"]])
            cards.append(card)
    return cards[:MAX_CARDS], max(0, len(cards) - MAX_CARDS)
