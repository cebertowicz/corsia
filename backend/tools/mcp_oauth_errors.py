"""Fixed failure classes for MCP sign-in.

Error text here is a class, never a provider body, token, code or URL. Only an
``unknown`` class may carry the provider's own short words, which the screen
shows under "What <app> said"; they are scrubbed and bounded first.
"""

from __future__ import annotations

from tools.mcp_oauth_secrets import redact

CLASSES = frozenset({
    "timed_out", "cancelled", "declined", "not_allowed", "permissions", "app_error", "app_busy",
    "address_mismatch", "unknown",
})
_BY_PROVIDER_CODE = {
    "access_denied": "declined",
    "unauthorized_client": "not_allowed",
    "unsupported_response_type": "not_allowed",
    "invalid_scope": "permissions",
    "server_error": "app_error",
    "temporarily_unavailable": "app_busy",
    "invalid_client": "address_mismatch",
    "redirect_uri_mismatch": "address_mismatch",
    "invalid_redirect_uri": "address_mismatch",
}
_WORDS_MAX = 200


class SignInError(Exception):
    """A sign-in ended without a grant. ``failure_class`` is one of :data:`CLASSES`."""

    def __init__(self, failure_class: str, words: str | None = None) -> None:
        if failure_class not in CLASSES:
            failure_class = "unknown"
        super().__init__(failure_class)
        self.failure_class = failure_class
        # Only an unknown class shows the provider's words.
        self.words = _clean_words(words) if failure_class == "unknown" else None


def _clean_words(words: str | None) -> str | None:
    if not isinstance(words, str):
        return None
    cleaned = " ".join(redact(words).split())[:_WORDS_MAX]
    return cleaned or None


def from_provider_error(code: object, description: object = None) -> SignInError:
    """Classify an OAuth ``error`` (and ``error_description``) the provider sent."""
    known = _BY_PROVIDER_CODE.get(code) if isinstance(code, str) else None
    if known:
        return SignInError(known)
    words = description if isinstance(description, str) and description.strip() else code
    return SignInError("unknown", words if isinstance(words, str) else None)
