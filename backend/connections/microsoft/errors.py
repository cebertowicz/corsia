"""Fixed, credential-free Microsoft sign-in failures."""
from __future__ import annotations

APP_REGISTRATION_NOT_ALLOWED = "app_registration_not_allowed"
ADMIN_APPROVAL_REQUIRED = "admin_approval_required"
CONSENT_CANCELLED = "consent_cancelled"
REFRESH_TOKEN_INVALID = "refresh_token_invalid"
PROVIDER_UNAVAILABLE = "provider_unavailable"
MANUAL_ONLY = "manual_only"
INPUT_REFUSAL = "input_refusal"

CAUSES = frozenset((
    APP_REGISTRATION_NOT_ALLOWED,
    ADMIN_APPROVAL_REQUIRED,
    CONSENT_CANCELLED,
    REFRESH_TOKEN_INVALID,
    PROVIDER_UNAVAILABLE,
    MANUAL_ONLY,
    INPUT_REFUSAL,
))


class MicrosoftError(Exception):
    """A safe Microsoft result; provider response text must never leave this package."""

    def __init__(self, cause: str, *, browser_message: str | None = None) -> None:
        if cause not in CAUSES:
            cause = PROVIDER_UNAVAILABLE
        super().__init__(cause)
        self.cause = cause
        self.browser_message = browser_message


def token_error_cause(status: int | None, payload: object = None) -> str:
    """Map Microsoft OAuth failures to the approved fixed vocabulary only."""
    code = payload.get("error") if isinstance(payload, dict) else None
    error_codes = payload.get("error_codes") if isinstance(payload, dict) else None
    aadsts_codes = {value for value in error_codes if isinstance(value, int) and not isinstance(value, bool)} if isinstance(error_codes, list) else set()
    # Microsoft confirms AADSTS65004 is declined consent, never administrator approval.
    if 65004 in aadsts_codes:
        return CONSENT_CANCELLED
    if aadsts_codes & {90094, 90095, 65001}:
        return ADMIN_APPROVAL_REQUIRED
    if code in {"authorization_pending", "temporarily_unavailable"}:
        return PROVIDER_UNAVAILABLE
    if code in {"invalid_grant", "invalid_client"}:
        return REFRESH_TOKEN_INVALID
    if code in {"access_denied", "consent_required", "interaction_required"}:
        return CONSENT_CANCELLED
    if status in (400, 401):
        return CONSENT_CANCELLED
    return PROVIDER_UNAVAILABLE
