"""Encode and recover CORSIA's private tool-invocation echo marker."""

from __future__ import annotations

import json


_INVOCATION_MARKER = "\n\n[CORSIA_TOOL_INVOCATION reference="


def with_invocation_reference(text: str, invocation_id: str) -> str:
    """Append the one echo marker ``invocation_id_from_echo`` reads back."""
    reference = json.dumps(
        {"invocation_id": invocation_id},
        sort_keys=True,
        separators=(",", ":"),
    )
    return f"{text}{_INVOCATION_MARKER}{reference}]"


def invocation_reference_from_echo(content: str) -> tuple[bool, str | None]:
    """Return (marker present, valid id); (True, None) means malformed provenance."""
    value = content or ""
    marker_at = value.rfind(_INVOCATION_MARKER)
    if marker_at < 0:
        return False, None
    if not value.endswith("]"):
        return True, None
    raw_reference = value[marker_at + len(_INVOCATION_MARKER):-1]
    try:
        reference = json.loads(raw_reference)
    except json.JSONDecodeError:
        return True, None
    if not isinstance(reference, dict) or set(reference) != {"invocation_id"}:
        return True, None
    invocation_id = reference.get("invocation_id")
    valid = isinstance(invocation_id, str) and bool(invocation_id.strip())
    return True, invocation_id if valid else None


def invocation_id_from_echo(content: str) -> str | None:
    """Read the final valid invocation id, preserving the optional-id interface."""
    return invocation_reference_from_echo(content)[1]
