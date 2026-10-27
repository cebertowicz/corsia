"""Microsoft sign-in's writes through the shared account store: the saved registration a row
signs in again with, and the row and credentials a finished sign-in creates or renews."""
from __future__ import annotations

import time
from typing import TYPE_CHECKING

from connections.connected_accounts import MICROSOFT_PROVIDER
from connections.microsoft import client
from connections.microsoft.config import SCOPES
from connections.microsoft.errors import (
    ACCOUNT_NOT_FOUND, ACCOUNT_REMOVED, ALREADY_CONNECTED, ALREADY_NEEDS_SIGN_IN, DIFFERENT_ACCOUNT, MicrosoftError,
)
from connections.sign_in_notice import wake_on_connect
from infra import key_store
from infra.database import get_db, write_transaction

if TYPE_CHECKING:
    from connections.microsoft.oauth import _Pending


def saved_registration(account_id: str, thread_id: str | None) -> tuple[str, str, str | None]:
    """The registration a Microsoft row signed in with, read server-side: ``(client_id, kind, tenant_id)``.

    An account the caller can't see gets exactly what an unknown one gets.
    """
    from connections.connected_accounts import account_client, visible_account

    db = get_db()
    try:
        account = visible_account(account_id, thread_id, db=db, provider=MICROSOFT_PROVIDER)
        saved = account_client(account_id, db=db) if account is not None else None
    finally:
        db.close()
    if account is None or saved is None or account["account_kind"] not in {"personal", "work"}:
        raise MicrosoftError(ACCOUNT_NOT_FOUND)
    kind = str(account["account_kind"])
    return saved["client_id"], kind, saved["tenant_id"] if kind == "work" else None


def provider_subject(identity: client.Identity) -> str:
    return f"{identity.tenant_id}:{identity.object_id}"


def store(pending: _Pending, grant: dict[str, object], identity: client.Identity) -> None:
    """Write only this flow's row through the shared store: create it (an add) or sign it in again."""
    from connections.connected_accounts import (
        ConnectedAccountError, create_account, create_account_for_thread, get_account, list_accounts, store_credentials,
    )
    from connections.account_state import mark_signed_in, record_audit

    subject = provider_subject(identity)
    expires_in = grant.get("expires_in")
    bundle = {
        "access_token": key_store.validated_value(grant["access_token"]),
        "access_token_expires_at": str(time.time() + expires_in)
        if isinstance(expires_in, (int, float)) and not isinstance(expires_in, bool) and expires_in > 0
        else str(time.time()),
        "refresh_token": key_store.validated_value(grant["refresh_token"]),
        "client_id": pending.client_id, "tenant_id": "consumers" if identity.kind == "personal" else identity.tenant_id,
        "scopes": list(SCOPES),
    }
    with write_transaction(None, scope="microsoft_sign_in_store") as conn:
        if pending.adding:
            existing = next(
                (row for row in list_accounts(MICROSOFT_PROVIDER, db=conn) if row["provider_subject"] == subject), None,
            )
            if existing is not None:
                raise MicrosoftError(
                    ALREADY_CONNECTED if existing["state"] == "connected" else ALREADY_NEEDS_SIGN_IN,
                    address=str(existing["address"]),
                )
            fields = {"created_by": pending.actor, "signed_in_by": pending.actor, "provider_subject": subject,
                      "account_kind": identity.kind, "account_id": pending.account_id, "state": "connected"}
            if pending.adding_thread_id is not None:
                create_account_for_thread(
                    MICROSOFT_PROVIDER, identity.address, actor_thread_id=pending.adding_thread_id, db=conn, **fields,
                )
            else:
                create_account(MICROSOFT_PROVIDER, identity.address, reach=pending.reach, db=conn, **fields)  # type: ignore[arg-type]
        else:
            try:
                account = get_account(pending.account_id, db=conn)
            except ConnectedAccountError:
                raise MicrosoftError(ACCOUNT_REMOVED) from None
            if account["provider_subject"] != subject:
                raise MicrosoftError(DIFFERENT_ACCOUNT, signed_in=identity.address, address=str(account["address"]))
            mark_signed_in(
                pending.account_id, address=identity.address, provider_subject=subject, account_kind=identity.kind,
                actor=pending.actor, db=conn,
            )
        store_credentials(pending.account_id, bundle, db=conn)
        record_audit(pending.account_id, "connect" if pending.adding else "sign_in", pending.actor, db=conn)
        wake_on_connect(
            conn, pending.adding_thread_id, pending.state, MICROSOFT_PROVIDER, pending.account_id, identity.address,
        )
