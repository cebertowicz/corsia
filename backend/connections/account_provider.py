"""What one provider contributes to the shared connected-account lifecycle.

Rows, reach, visibility, state, credentials storage, audit, Sign out, Remove
and Where it works are provider-neutral (``connections.connected_accounts``
and ``connections.account_actions``). A provider supplies only these facts and
hooks; its OAuth, identity parsing and API clients stay in its own package.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from dataclasses import dataclass

from connections.account_state import MANUAL_ONLY

#: The audit cause when a provider has no app-only revocation to call.
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

    @property
    def required_fields(self) -> frozenset[str]:
        return self.bundle_fields | ({SCOPES_FIELD} if self.scopes_required else frozenset())

    def valid_bundle(self, bundle: object, required: frozenset[str] | None = None) -> bool:
        """A stored bundle: ``required`` (default: a usable sign-in) present, nothing unknown."""
        needed = self.required_fields if required is None else required
        if not isinstance(bundle, dict) or not needed <= set(bundle) or set(bundle) - (self.bundle_fields | {SCOPES_FIELD}):
            return False
        if any(not isinstance(bundle[key], str) or not bundle[key] for key in set(bundle) & self.bundle_fields):
            return False
        scopes = bundle.get(SCOPES_FIELD, [])
        return isinstance(scopes, list) and all(isinstance(scope, str) for scope in scopes)
