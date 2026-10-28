"""Google's part of the shared connected-account lifecycle: its bundle shape and its revoke call."""

from __future__ import annotations

import httpx

from connections.account_provider import AccountProvider
from connections.google.client import TIMEOUT_SECONDS, uses_published

REVOKE_URL = "https://oauth2.googleapis.com/revoke"
#: The revoke results that show the person the manual Google Account step: Google could not be
#: asked, or refused. ``revoked`` shows nothing more; ``not_signed_in`` means Sign out already revoked it.
MANUAL_STEP_CAUSES = frozenset(("provider_unreachable", "provider_timeout", "provider_rejected", "provider_unexpected"))


async def revoke(refresh_token: str | None) -> str:
    """Revoke one refresh token at Google; return the fixed audit cause, never Google's text."""
    if not refresh_token:
        return "not_signed_in"
    try:
        async with httpx.AsyncClient(timeout=TIMEOUT_SECONDS) as http:
            response = await http.post(REVOKE_URL, data={"token": refresh_token})
    except (httpx.ConnectError, httpx.ConnectTimeout, httpx.PoolTimeout):
        return "provider_unreachable"
    except httpx.TimeoutException:
        return "provider_timeout"
    except httpx.HTTPError:
        return "provider_unexpected"
    if response.status_code == 200:
        return "revoked"
    return "provider_rejected" if 400 <= response.status_code < 500 else "provider_unexpected"


async def _release(account_id: str) -> None:
    """Stop the removed account's in-flight inbox check."""
    from events.connectors import mail_poller  # Imported here: the poller imports the account store.

    mail_poller.cancel_account(account_id)


GOOGLE_BUNDLE_FIELDS = frozenset(("client_id", "client_secret", "refresh_token"))


def _required_for(bundle: dict) -> frozenset[str]:
    return GOOGLE_BUNDLE_FIELDS - {"client_secret"} if uses_published(bundle) else GOOGLE_BUNDLE_FIELDS


GOOGLE = AccountProvider(
    name="google", display_name="Google", capabilities=("mail", "calendar", "files"),
    bundle_fields=GOOGLE_BUNDLE_FIELDS, scopes_required=False, required_for=_required_for,
    client_fields=frozenset(("client", "client_id", "client_secret")), optional_bundle_fields=frozenset(("client",)),
    revoke=revoke, release=_release,
)
