"""User-facing external harness model names, one rule shared with ``modelDisplayName.ts``."""
from __future__ import annotations

import re


def _claude_display_name(words: list[str]) -> str:
    """Title-case Claude words, drop a trailing 8-digit date, join a one-digit pair as d.d."""
    if len(words) > 1 and re.fullmatch(r"\d{8}", words[-1]):
        words = words[:-1]
    name = " ".join(word[:1].upper() + word[1:] for word in words)
    return re.sub(r"\b(\d) (\d)\b", r"\1.\2", name)


def external_model_display_name(model: str | None) -> str | None:
    """Return a concise user-facing external harness model name.

    Shared with ``modelDisplayName.ts`` and run over
    ``testdata/model-display-names.json`` on both sides. The input is a model id.
    """
    if not model:
        return None
    name = model.split("/", 1)[1] if "/" in model else model
    if name.startswith("claude"):
        return _claude_display_name(name.split("-"))
    if name == "gpt-5-codex":
        return "GPT-5 Codex"
    if name.startswith("gpt-"):
        return "GPT-" + name.removeprefix("gpt-").replace("-", " ")
    return name.replace("-", " ").title()
