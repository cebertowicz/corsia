"""Shared utility for handling <think>...</think> tags in LLM output.

Some models (Qwen, DeepSeek, o1) embed reasoning in content using
<think> tags rather than a dedicated reasoning_content field.
"""

from __future__ import annotations

import re

_THINK_RE = re.compile(r"<think>([\s\S]*?)</think>")


def strip_think_tags(text: str | None) -> str | None:
    """Remove <think>...</think> blocks from text. Returns None if result is empty."""
    if not text:
        return None
    return _THINK_RE.sub("", text).strip() or None


def extract_think_content(text: str | None) -> str | None:
    """Extract content from first <think>...</think> block. Returns None if not found."""
    if not text:
        return None
    match = _THINK_RE.search(text)
    return match.group(1).strip() if match else None
