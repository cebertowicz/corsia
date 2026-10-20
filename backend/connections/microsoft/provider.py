"""Microsoft's part of the shared connected-account lifecycle.

A row is identified by issuing tenant plus object id and is ``personal`` or
``work``. Microsoft has no app-only revocation (and ``revokeSignInSessions``
would end the person's sessions everywhere), so Sign out and Remove are local
and the audit records ``manual_only``. Sign out keeps the client and tenant ids
so the row can sign in again with the same registration.
"""

from __future__ import annotations

import uuid

from connections.account_provider import AccountProvider
from connections.microsoft.config import SCOPES


def _guid(value: object) -> str | None:
    try:
        return str(uuid.UUID(value)) if isinstance(value, str) else None
    except ValueError:
        return None


def _registration(client: dict[str, str]) -> tuple[str | None, str | None]:
    """The Application (client) ID and, for a work account, its Directory (tenant) ID; a personal
    account's ``consumers`` tenant is no directory, so it shows none."""
    return _guid(client.get("client_id")), _guid(client.get("tenant_id"))


async def _release(account_id: str) -> None:
    """Close the removed account's waiting sign-in and its loopback listener."""
    from connections.microsoft.oauth import clear_pending  # Imported here: oauth imports the account store.
    from events.connectors.mail_poller import cancel_account

    cancel_account(account_id)
    await clear_pending(account_id)


MICROSOFT = AccountProvider(
    name="microsoft", display_name="Microsoft", capabilities=("mail", "calendar", "files"),
    bundle_fields=frozenset(("access_token", "refresh_token", "client_id", "tenant_id")), scopes_required=True,
    optional_bundle_fields=frozenset(("access_token_expires_at",)),
    client_fields=frozenset(("client_id", "tenant_id")), account_kinds=frozenset(("personal", "work")),
    subject_required=True, release=_release, registration=_registration, display_scopes=SCOPES,
)
