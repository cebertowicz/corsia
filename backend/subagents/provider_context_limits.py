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


# oMLX refuses a prompt whose prefill cannot fit in memory: "oMLX prefill memory
# guard rejected this prompt: ...", whichever of its guards refused. Anchored on
# the refusal verb so a settings label such as "Memory Guard: aggressive" does
# not classify.
_OMLX_GUARD_MARKER = "prefillmemoryguardrejected"


def _normalized(value: Any) -> str:
    normalized = _flatten(value).lower().replace("_", "").replace("-", "")
    return "".join(normalized.split())


def is_omlx_guard_refusal(value: Any) -> bool:
    """Return true for an oMLX prefill memory-guard refusal, in any of its wordings."""
    return _OMLX_GUARD_MARKER in _normalized(value)


def is_provider_context_limit(value: Any) -> bool:
    """Return true for provider errors that mean the prompt exceeded context."""
    normalized = _normalized(value)
    markers = (
        "contextwindowexceeded",
        "contextlengthexceeded",
        # oMLX/OpenAI rejection wording: "This model's maximum context length
        # is N tokens, however you requested M tokens". The assertion form
        # ("... is N") distinguishes a rejection from a benign capability
        # mention ("a maximum context length of N tokens"), which must not
        # classify.
        "maximumcontextlengthis",
        _OMLX_GUARD_MARKER,
        "promptistoolong",
        "prompttoolong",
        "ranoutofroom",
        "toomanytokens",
    )
    return any(marker in normalized for marker in markers)
