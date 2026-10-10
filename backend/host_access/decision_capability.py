"""Process-local capabilities for user-only host-access decisions."""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import secrets
from typing import Any, Mapping


INTERACTIVE_UI_HEADER = "X-CORSIA-Interactive-UI"
USER_DECISION_HEADER = "X-CORSIA-User-Decision"
_CAPABILITY_VERSION = 1
_CAPABILITY_KEY = secrets.token_bytes(32)


def _purpose(state: object) -> str | None:
    if state == "awaiting_user":
        return "pending-decision"
    if state == "active":
        return "revoke"
    return None


def _payload(
    lease: Mapping[str, Any],
    *,
    user_id: str,
    purpose: str,
) -> bytes:
    value = {
        "version": _CAPABILITY_VERSION,
        "purpose": purpose,
        "user_id": user_id,
        "lease_id": lease.get("id"),
        "revision": lease.get("revision"),
        "application_hash": lease.get("application_hash"),
    }
    return json.dumps(
        value,
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
    ).encode("utf-8")


def issue_user_decision_capability(
    lease: Mapping[str, Any],
    *,
    user_id: str,
) -> str | None:
    """Issue a restart-scoped capability for one pending decision or revoke."""
    purpose = _purpose(lease.get("state"))
    if purpose is None:
        return None
    digest = hmac.new(
        _CAPABILITY_KEY,
        _payload(lease, user_id=user_id, purpose=purpose),
        hashlib.sha256,
    ).digest()
    return base64.urlsafe_b64encode(digest).rstrip(b"=").decode("ascii")


def verify_user_decision_capability(
    capability: str | None,
    lease: Mapping[str, Any],
    *,
    user_id: str,
    operation: str,
) -> bool:
    """Verify a capability without exposing the process-local signing key."""
    purpose = "revoke" if operation == "revoke" else "pending-decision"
    if not isinstance(capability, str) or not capability:
        return False
    expected = hmac.new(
        _CAPABILITY_KEY,
        _payload(lease, user_id=user_id, purpose=purpose),
        hashlib.sha256,
    ).digest()
    try:
        supplied = base64.urlsafe_b64decode(capability + ("=" * (-len(capability) % 4)))
    except (ValueError, TypeError):
        return False
    return hmac.compare_digest(supplied, expected)


def attach_user_decision_capability(
    lease: Mapping[str, Any],
    *,
    user_id: str,
) -> dict[str, Any]:
    """Return UI state with a capability only when a user decision is possible."""
    result = dict(lease)
    capability = issue_user_decision_capability(result, user_id=user_id)
    if capability is not None:
        result["decision_capability"] = capability
    return result
