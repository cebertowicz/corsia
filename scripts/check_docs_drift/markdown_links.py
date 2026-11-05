"""Check relative Markdown links in the documentation tree using stdlib only."""

from __future__ import annotations

import html
import re
import unicodedata
from collections import defaultdict
from pathlib import Path
from urllib.parse import unquote, urlsplit

_LINK = re.compile(r"!?\[[^\]\n]*\]\(\s*(?:<([^>\n]+)>|([^\s)]+))(?:\s+['\"][^\n]*?['\"])?\s*\)")
_HEADING = re.compile(r"^ {0,3}#{1,6}\s+(.+?)\s*#*\s*$")
_FENCE = re.compile(r"^ {0,3}(`{3,}|~{3,})")
_INLINE_CODE = re.compile(r"(`+).*?\1")
_REFERENCE = re.compile(r"^ {0,3}\[([^]]+)\]:\s*(?:<([^>]+)>|(\S+))")
_REFERENCE_USE = re.compile(r"!?\[([^]\n]+)\]\[([^]\n]*)\]")


def _prose(text: str) -> list[str]:
    """Discard fenced code; it describes examples, not rendered links/headings."""
    lines = []
    fence = None
    for line in text.splitlines():
        marker = _FENCE.match(line)
        if marker:
            token = marker.group(1)
            if fence is None:
                fence = token
            elif token[0] == fence[0] and len(token) >= len(fence):
                fence = None
            continue
        if fence is None:
            lines.append(line)
    return lines


def _slug(heading: str) -> str:
    heading = _LINK.sub(lambda m: m.group(0).split("]", 1)[0].lstrip("!["), heading)
    heading = re.sub(r"<[^>]*>", "", heading)
    heading = html.unescape(heading).lower()
    return "".join(
        "-" if char == " " else char
        for char in heading
        if char == " " or char in "-_" or unicodedata.category(char)[0] not in "PCSZ"
    )


def _anchors(path: Path) -> set[str]:
    seen: dict[str, int] = defaultdict(int)
    anchors = set()
    lines = _prose(path.read_text(encoding="utf-8"))
    for index, line in enumerate(lines):
        match = _HEADING.match(line)
        heading = match.group(1) if match else None
        if heading is None and index and re.fullmatch(r" {0,3}(?:=+|-+)\s*", line):
            heading = lines[index - 1].strip() or None
        if heading is None:
            continue
        base = _slug(heading)
        slug = base
        while slug in anchors:
            seen[base] += 1
            slug = f"{base}-{seen[base]}"
        anchors.add(slug)
    return anchors


def _destinations(lines: list[str]) -> list[str]:
    references = {}
    for line in lines:
        match = _REFERENCE.match(line)
        if match:
            references[match.group(1).casefold()] = match.group(2) or match.group(3)
    destinations = []
    for line in lines:
        if _REFERENCE.match(line):
            continue
        line = _INLINE_CODE.sub("", line)
        destinations.extend(m.group(1) or m.group(2) for m in _LINK.finditer(line))
        for match in _REFERENCE_USE.finditer(line):
            target = references.get((match.group(2) or match.group(1)).casefold())
            if target:
                destinations.append(target)
    return destinations


def check_links(repo_root: Path) -> list[str]:
    """Return source page, destination and cause for broken relative links."""
    failures = []
    for doc in sorted((repo_root / "docs").rglob("*.md")):
        lines = _prose(doc.read_text(encoding="utf-8"))
        for raw in _destinations(lines):
            destination = html.unescape(raw)
            url = urlsplit(destination)
            if url.scheme or url.netloc or url.path.startswith("/"):
                continue
            target = (doc.parent / unquote(url.path)) if url.path else doc
            reason = None
            if not target.is_file():
                reason = "target file missing"
            elif url.fragment and unquote(url.fragment) not in _anchors(target):
                reason = "heading anchor missing"
            if reason:
                failures.append(f"{doc.relative_to(repo_root)}: {destination}: {reason}")
    return failures
