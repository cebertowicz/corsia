"""The mail-host part of the shared connected-account lifecycle.

A row is one address on one host. Its credential is the app password the person pasted into
Settings › Keys, held raw as that entry's value; the servers are in the row's ``endpoints_json``.
There is nothing to revoke from here (the person deletes the app password at the host), so
Sign out and Remove are local and the audit records ``manual_only``.
"""

from __future__ import annotations

from connections.account_provider import AccountProvider
from connections.mailhost.row_detail import row_detail
from infra.key_store import MAILHOST_PREFIX

PASSWORD = "password"

MAILHOST = AccountProvider(
    name="mailhost", display_name="Email", capabilities=("mail", "calendar"),
    bundle_fields=frozenset((PASSWORD,)), scopes_required=False, client_fields=frozenset(),
    subject_required=True, raw_credential_field=PASSWORD, credential_prefix=MAILHOST_PREFIX, row_detail=row_detail,
)
