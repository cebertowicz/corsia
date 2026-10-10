"""Stable identity for exact model-facing CORSIA tool definitions."""

from __future__ import annotations

import hashlib
import json


def definitions_fingerprint(definitions) -> str:
    """Hash one tool surface independent of definition list ordering."""
    normalized = sorted(
        definitions,
        key=lambda item: item.get("function", item).get("name", ""),
    )
    encoded = json.dumps(
        normalized,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
        allow_nan=False,
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()
