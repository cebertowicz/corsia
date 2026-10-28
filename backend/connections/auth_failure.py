"""Classify provider errors that conclusively require a new sign-in."""
from __future__ import annotations

SIGN_IN_EXPIRED = "sign_in_expired"


def auth_failure_kind(provider: str, detail: str | BaseException | None) -> str | None:
    """Return the fixed, provider-specific auth failure kind, never infer from a bare 401.

    Claude Code can retain a credential file after its OAuth refresh becomes
    impossible. Its exact terminal error is stronger evidence than a generic
    HTTP authentication response; transient 401s remain retryable.
    """
    if provider != "claude-code" or detail is None:
        return None
    text = str(detail).casefold()
    if "not logged in · please run /login" in text:
        return SIGN_IN_EXPIRED
    required = (
        "failed to authenticate",
        "oauth session expired",
        "could not be refreshed",
    )
    return SIGN_IN_EXPIRED if all(part in text for part in required) else None
