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
from collections.abc import Collection, Sequence
from pathlib import Path
from urllib.parse import unquote

from registry.chat_cards import reference_card
from registry.path_access import lookup_file

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


def reference_cards(
    db, text: str, bases: Sequence[Path], user_id: str, *, skip: Collection[str] = frozenset(),
) -> tuple[list[dict], int]:
    """Cards for the registered artifacts ``text`` names, and how many more past the cap.

    ``skip`` holds artifact ids already shown as an ask card in the same message:
    an artifact is one card, and the ask is the one that carries state.
    """
    roots = [str(base) for base in bases]
    cards: list[dict] = []
    seen: set[str] = set(skip)
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
            cards.append(card)
    return cards[:MAX_CARDS], max(0, len(cards) - MAX_CARDS)
