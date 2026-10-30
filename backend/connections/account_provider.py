"""What one provider contributes to the shared connected-account lifecycle.

Rows, reach, visibility, state, credentials storage, audit, Sign out, Remove
and Where it works are provider-neutral (``connections.connected_accounts``
and ``connections.account_actions``). A provider supplies only these facts and
hooks; its OAuth, identity parsing and API clients stay in its own package.
"""

from __future__ import annotations

import sqlite3
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Any

#: The credential-bundle field holding granted or requested scope names.
SCOPES_FIELD = "scopes"


@dataclass(frozen=True)
class AccountProvider:
    name: str
    #: The provider as people read it: row titles and refusals say "<display_name> account".
    display_name: str
    capabilities: tuple[str, ...]
    #: The non-empty string fields a usable credential bundle holds.
    bundle_fields: frozenset[str]
    #: Whether a usable bundle must list its granted scopes (otherwise the list is optional).
    scopes_required: bool
    #: The bundle fields Sign out keeps, so signing in again needs no new setup.
    client_fields: frozenset[str]
    #: Optional stored fields accepted when present but not required for an older usable bundle.
    optional_bundle_fields: frozenset[str] = frozenset()
    #: Account kinds the provider tells apart; empty when it has none.
    account_kinds: frozenset[str] = frozenset()
    #: Whether every row must carry the provider's stable account identity.
    subject_required: bool = False
    #: Revoke a signed-out or removed refresh token at the provider, returning the fixed audit
    #: cause. None when the provider has no app-only revocation: the audit records ``manual_only``.
    revoke: Callable[[str | None], Awaitable[str]] | None = None
    #: Stop this process's work for a removed account (an inbox check, a waiting sign-in).
    release: Callable[[str], Awaitable[None]] | None = None
    #: The person's own registration behind one account, from its saved client fields, as the
    #: ``(client_id, tenant_id)`` GUIDs its row may show (each None when it has none). None when
    #: the provider has no per-account registration: the row shows neither.
    registration: Callable[[dict[str, str]], tuple[str | None, str | None]] | None = None
    #: The stable scope names a row may display; configured by the provider rather than duplicated in a UI.
    display_scopes: tuple[str, ...] = ()
    #: Set when the credential is the account's key entry value as typed, not a JSON bundle: it is
    #: exposed as a one-field bundle under this name (an app password).
    raw_credential_field: str | None = None
    #: The key-store name prefix of this provider's accounts; None keeps the shared one.
    credential_prefix: str | None = None
    #: Extra fields this provider's list row carries, from ``(account_id, db)``: its own label for the
    #: row and whatever its open row needs. Never a credential, a key value or a host's reply text.
    row_detail: Callable[[str, sqlite3.Connection], dict[str, Any]] | None = None
    #: Batched row details keyed by account id. Providers without one use ``row_detail`` per row.
    row_details: Callable[[list[str], sqlite3.Connection], dict[str, dict[str, Any]]] | None = None

    @property
    def required_fields(self) -> frozenset[str]:
        return self.bundle_fields | ({SCOPES_FIELD} if self.scopes_required else frozenset())

    def valid_bundle(self, bundle: object, required: frozenset[str] | None = None) -> bool:
        """A stored bundle: ``required`` (default: a usable sign-in) present, nothing unknown."""
        needed = self.required_fields if required is None else required
        allowed = self.bundle_fields | self.optional_bundle_fields | {SCOPES_FIELD}
        if not isinstance(bundle, dict) or not needed <= set(bundle) or set(bundle) - allowed:
            return False
        if any(
            not isinstance(bundle[key], str) or not bundle[key]
            for key in set(bundle) & (self.bundle_fields | self.optional_bundle_fields)
        ):
            return False
        scopes = bundle.get(SCOPES_FIELD, [])
        return isinstance(scopes, list) and all(isinstance(scope, str) for scope in scopes)
