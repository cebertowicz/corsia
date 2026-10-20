"""Fixed, credential-free Microsoft sign-in results.

Every failure is one fixed code with a fixed message (for an agent or the
API) and a fixed browser-page text; provider response text never reaches any
of them. Only the codes in ``STATE_CAUSES`` describe the account's state and
are recorded on its row. Other fixed codes can appear as the latest failed add,
but do not alter a row's state.
"""
from __future__ import annotations

from connections.connected_accounts import (
    ADMIN_APPROVAL_REQUIRED, CONSENT_CANCELLED, PROVIDER_UNAVAILABLE, REFRESH_TOKEN_INVALID,
    REGISTRATION_MISCONFIGURED, STATE_CAUSES,
)

INPUT_REFUSAL = "input_refusal"
ACCOUNT_NOT_FOUND = "account_not_found"
SIGN_IN_BUSY = "sign_in_busy"
ALREADY_CONNECTED = "already_connected"
ALREADY_NEEDS_SIGN_IN = "already_needs_sign_in"
DIFFERENT_ACCOUNT = "different_account"
ACCOUNT_REMOVED = "account_removed"
INTERNAL = "internal"

_BROWSER_RETRY = "Microsoft sign-in couldn't finish. Go back to CORSIA and try again."
# (message, browser page) per code; ``{address}`` and ``{signed_in}`` are filled from the account.
_TEXT: dict[str, tuple[str, str]] = {
    REFRESH_TOKEN_INVALID: (
        "Microsoft rejected the saved sign-in. Use Sign in on this account's row in Connected apps.",
        _BROWSER_RETRY,
    ),
    ADMIN_APPROVAL_REQUIRED: (
        "Your organization needs to approve CORSIA before it can connect this account.",
        "Your organization needs to approve CORSIA before it can connect this account. Go back to CORSIA.",
    ),
    CONSENT_CANCELLED: (
        "Microsoft sign-in was cancelled. Call microsoft_connect for a new link.",
        "Microsoft sign-in was cancelled. Go back to CORSIA and try again.",
    ),
    PROVIDER_UNAVAILABLE: (
        "Microsoft had a problem with sign-in. Call microsoft_connect for a new link.",
        "Microsoft had a problem with sign-in. Go back to CORSIA and try again.",
    ),
    REGISTRATION_MISCONFIGURED: (
        "The person's own Microsoft app registration isn't configured correctly. Check that the app exists in the registration "
        "tenant and its supported account type matches this connection; Authentication has http://localhost under Mobile "
        "and desktop applications and Allow public client flows is Yes; and Microsoft Graph delegated permissions are "
        "Mail.ReadWrite, Mail.Send, Calendars.ReadWrite, Files.ReadWrite, offline_access, and User.Read. Do not create "
        "a client secret. Fix the registration through the microsoft-setup steps, then call microsoft_connect again.",
        "Your Microsoft app for CORSIA isn’t set up correctly. Go back to CORSIA and ask it in chat to fix the app.",
    ),
    INPUT_REFUSAL: (
        "microsoft_connect needs client_id as the Application (client) ID GUID, kind as personal, work or both, "
        "and tenant_id as the Directory (tenant) ID GUID for work only.",
        _BROWSER_RETRY,
    ),
    ACCOUNT_NOT_FOUND: (
        "No Microsoft account has that account_id. Call microsoft_status to list them.",
        _BROWSER_RETRY,
    ),
    SIGN_IN_BUSY: (
        "Too many Microsoft sign-ins are waiting. Finish one or wait ten minutes, then call microsoft_connect.",
        "Too many Microsoft sign-ins are waiting. Go back to CORSIA and try again later.",
    ),
    ALREADY_CONNECTED: (
        "{address} is already connected. To change where it works, use Where it works… on its row in Connected apps.",
        "{address} is already connected. To change where it works, use Where it works… on its row in Connected apps.",
    ),
    ALREADY_NEEDS_SIGN_IN: (
        "{address} is already connected, but it needs you to sign in again. Use Sign in on its row in Connected apps.",
        "{address} is already connected, but it needs you to sign in again. Use Sign in on its row in Connected apps.",
    ),
    DIFFERENT_ACCOUNT: (
        "You signed in as {signed_in}, but this sign-in is for {address}. Call microsoft_connect again and "
        "sign in as {address}.",
        "That's a different Microsoft account. Go back to CORSIA and sign in again as {address}.",
    ),
    ACCOUNT_REMOVED: (
        "That Microsoft account was removed during its sign-in. Call microsoft_connect to add it again.",
        "This Microsoft account was removed from CORSIA. Go back to CORSIA and connect it again.",
    ),
    INTERNAL: (
        "Microsoft sign-in couldn't finish. Call microsoft_connect for a new link.",
        _BROWSER_RETRY,
    ),
}
CODES = frozenset(_TEXT)


class MicrosoftError(Exception):
    """A safe Microsoft result; provider response text must never leave this package."""

    def __init__(self, cause: str, **fields: str) -> None:
        if cause not in _TEXT:
            raise ValueError(f"unknown Microsoft sign-in result {cause!r}")
        message, browser = _TEXT[cause]
        super().__init__(cause)
        self.cause = cause
        self.message = message.format(**fields)
        self.browser_message = browser.format(**fields)

    @property
    def state_cause(self) -> str | None:
        """The cause to record on a row when this failure describes the account's state."""
        return self.cause if self.cause in STATE_CAUSES else None


def message(cause: str) -> str:
    """The fixed message for a recorded cause."""
    return _TEXT[cause][0]


def token_error_cause(status: int, payload: object) -> str:
    """Map a refresh refusal to fixed causes without exposing provider text."""
    error = payload.get("error") if isinstance(payload, dict) else None
    if status in (400, 401) and error in ("invalid_grant", "invalid_client", "unauthorized_client"):
        return REFRESH_TOKEN_INVALID
    return PROVIDER_UNAVAILABLE


_REGISTRATION_AADSTS_CODES = frozenset((50011, 700016, 7000218))
_REGISTRATION_OAUTH_ERRORS = frozenset(("invalid_client", "unauthorized_client"))


def exchange_error_cause(payload: object) -> str:
    """Map a failed authorization-code exchange to a fixed cause, never Microsoft's text.

    Consent and administrator approval take precedence over documented registration
    errors. Other failures remain provider-unavailable, so a retry is offered only
    when it can plausibly help.
    """
    error_codes = payload.get("error_codes") if isinstance(payload, dict) else None
    codes = {value for value in error_codes if isinstance(value, int) and not isinstance(value, bool)} if isinstance(error_codes, list) else set()
    error = payload.get("error") if isinstance(payload, dict) else None
    return redirect_codes_cause(codes, cancelled=False, oauth_error=error if isinstance(error, str) else None)


def redirect_codes_cause(aadsts_codes: set[int], *, cancelled: bool, oauth_error: str | None = None) -> str:
    """The fixed cause for a failure carrying these AADSTS codes."""
    if cancelled or 65004 in aadsts_codes:
        return CONSENT_CANCELLED
    if aadsts_codes & {90094, 90095, 65001}:
        return ADMIN_APPROVAL_REQUIRED
    if aadsts_codes & _REGISTRATION_AADSTS_CODES or oauth_error in _REGISTRATION_OAUTH_ERRORS:
        return REGISTRATION_MISCONFIGURED
    return PROVIDER_UNAVAILABLE
