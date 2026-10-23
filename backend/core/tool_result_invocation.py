"""Encode and recover CORSIA's private tool-invocation echo marker."""

from __future__ import annotations

import json


_INVOCATION_MARKER = "\n\n[CORSIA_TOOL_INVOCATION reference="
_REFERENCE = json.JSONDecoder()


def with_invocation_reference(text: str, invocation_id: str) -> str:
    """Append the one echo marker ``invocation_reference_from_echo`` reads back."""
    reference = json.dumps(
        {"invocation_id": invocation_id},
        sort_keys=True,
        separators=(",", ":"),
    )
    return f"{text}{_INVOCATION_MARKER}{reference}]"


def invocation_reference_from_echo(content: str) -> tuple[bool, str | None]:
    """Return (marker present, valid id); (True, None) means malformed provenance.

    The last marker is CORSIA's: it is written after everything the tool
    returned. A harness may append blocks of its own after it (Claude Code
    follows an image with a note), so the marker need not end the echo.
    """
    value = content or ""
    marker_at = value.rfind(_INVOCATION_MARKER)
    if marker_at < 0:
        return False, None
    try:
        reference, end = _REFERENCE.raw_decode(
            value, marker_at + len(_INVOCATION_MARKER),
        )
    except json.JSONDecodeError:
        return True, None
    if value[end:end + 1] != "]":
        return True, None
    if not isinstance(reference, dict) or set(reference) != {"invocation_id"}:
        return True, None
    invocation_id = reference.get("invocation_id")
    valid = isinstance(invocation_id, str) and bool(invocation_id.strip())
    return True, invocation_id if valid else None
