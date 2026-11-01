"""Shared provider context-limit classification for external harnesses."""

from __future__ import annotations

import json
from typing import Any


def _flatten(value: Any) -> str:
    if value is None:
        return ""
    if isinstance(value, str):
        return value
    try:
        return json.dumps(value, sort_keys=True, default=str)
    except TypeError:
        return str(value)


def is_provider_context_limit(value: Any) -> bool:
    """Return true for provider errors that mean the prompt exceeded context."""
    normalized = _flatten(value).lower().replace("_", "").replace("-", "")
    normalized = "".join(normalized.split())
    markers = (
        "contextwindowexceeded",
        "contextlengthexceeded",
        # oMLX/OpenAI rejection wording: "This model's maximum context length
        # is N tokens, however you requested M tokens". The assertion form
        # ("... is N") distinguishes a rejection from a benign capability
        # mention ("a maximum context length of N tokens"), which must not
        # classify.
        "maximumcontextlengthis",
        # oMLX refuses a prompt whose prefill cannot fit in memory: "oMLX prefill
        # memory guard rejected this prompt: ...". Anchored on the refusal verb so
        # a settings label such as "Memory Guard: aggressive" does not classify.
        "prefillmemoryguardrejected",
        "promptistoolong",
        "prompttoolong",
        "ranoutofroom",
        "toomanytokens",
    )
    return any(marker in normalized for marker in markers)
