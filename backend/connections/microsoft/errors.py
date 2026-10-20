"""Fixed, credential-free Microsoft sign-in results.

Every failure is one fixed code with a fixed message (for an agent or the
API) and a fixed browser-page text; provider response text never reaches any
of them. Only the codes in ``STATE_CAUSES`` describe the account's state and
are recorded on its row or as the latest failed add; the rest describe the
request (``input_refusal``), the account (``account_not_found``,
``already_connected``…) or CORSIA itself (``internal``).
"""
from __future__ import annotations

from connections.connected_accounts import (
    ADMIN_APPROVAL_REQUIRED, CONSENT_CANCELLED, PROVIDER_UNAVAILABLE, STATE_CAUSES,
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
        """The cause to record on the row or the latest add, when this failure describes the account's state."""
        return self.cause if self.cause in STATE_CAUSES else None


def message(cause: str) -> str:
    """The fixed message for a recorded cause."""
    return _TEXT[cause][0]


def exchange_error_cause(payload: object) -> str:
    """Map a failed authorization-code exchange to a fixed cause, never Microsoft's text.

    Only consent and administrator approval are told apart, by integer AADSTS code
    (AADSTS65004 is declined consent, never administrator approval). Anything else,
    ``invalid_client`` and ``invalid_grant`` included, is a sign-in Microsoft
    didn't complete: trying again is the person's only lever.
    """
    error_codes = payload.get("error_codes") if isinstance(payload, dict) else None
    codes = {value for value in error_codes if isinstance(value, int) and not isinstance(value, bool)} if isinstance(error_codes, list) else set()
    return redirect_codes_cause(codes, cancelled=False)


def redirect_codes_cause(aadsts_codes: set[int], *, cancelled: bool) -> str:
    """The fixed cause for a failure carrying these AADSTS codes."""
    if cancelled or 65004 in aadsts_codes:
        return CONSENT_CANCELLED
    if aadsts_codes & {90094, 90095, 65001}:
        return ADMIN_APPROVAL_REQUIRED
    return PROVIDER_UNAVAILABLE
