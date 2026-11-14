"""The sidebar preview line of a stored thread update: a marked message's first readable line, or a lifecycle label."""

from __future__ import annotations

import re

from infra.chat_comment_answers import strip_answer_tags

_INLINE_MARKDOWN = (
    re.compile(r"!?\[([^\]]+)\]\([^)]+\)"),
    re.compile(r"\*\*(?=\S)(.+?)(?<=\S)\*\*"),
    re.compile(r"__(?=\S)(.+?)(?<=\S)__"),
    re.compile(r"~~(?=\S)(.+?)(?<=\S)~~"),
    re.compile(r"(?<!\*)\*(?=\S)(.+?)(?<=\S)\*(?!\*)"),
    re.compile(r"(?<![\w_])_(?=\S)(.+?)(?<=\S)_(?![\w_])"),
    re.compile(r"`(?=\S)(.+?)(?<=\S)`"),
)


def _strip_inline_markdown(line: str) -> str:
    """Remove bounded inline Markdown while preserving ordinary punctuation."""
    previous = None
    while line != previous:
        previous = line
        for pattern in _INLINE_MARKDOWN:
            line = pattern.sub(r"\1", line)
    return line


def _first_readable_line(content: object) -> str | None:
    """The first non-code line of a marked message, without common Markdown chrome."""
    if not isinstance(content, str):
        return None
    in_fence = False
    for raw_line in content.splitlines():
        line = raw_line.strip()
        if line.startswith(("```", "~~~")):
            in_fence = not in_fence
            continue
        if in_fence or not line:
            continue
        line = re.sub(r"^(?:(?:#{1,6}|[-+*]|\d+[.)])\s+|>\s*)+", "", line)
        line = re.sub(r"^\[[ xX]\]\s+", "", line)
        line = _strip_inline_markdown(line).strip()
        if line:
            return line
    return None



def _summary(update: dict, message_content: object) -> str | None:
    """A stored event's sidebar preview; lifecycle text comes only from its snapshot."""
    if update["kind"] == "marked":
        return _first_readable_line(strip_answer_tags(message_content))
    title = update["title"]
    if not title:
        return None
    labels = {
        "done": "Completed",
        "done_follow_up": "Completed",
        "closed": "Closed",
        "failed": "Failed",
        "blocked": "Blocked",
    }
    label = labels.get(update["kind"])
    if label is None:
        return None
    suffix = " · Follow-up added" if update["kind"] == "done_follow_up" else ""
    return f"{label}: {title}{suffix}"
