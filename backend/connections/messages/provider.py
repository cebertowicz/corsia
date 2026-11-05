"""The Messages part of the shared connected-account lifecycle.

A row means "agents may read and send texts on this Mac". There is no sign-in and no credential:
the tools read the local Messages database and ask the Messages app to send, so the row only
carries Where it works. Sign out and Remove are local and the audit records ``manual_only``.
"""

from __future__ import annotations

from connections.account_provider import AccountProvider

MESSAGES = AccountProvider(
    name="messages", display_name="Messages", capabilities=("messages",),
    bundle_fields=frozenset(), scopes_required=False, client_fields=frozenset(), keyless=True,
)
