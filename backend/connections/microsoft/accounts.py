"""Microsoft connected-account persistence and visibility."""
from __future__ import annotations

import json
import sqlite3
import uuid

from connections.app_lifecycle_support import _assert_selected_projects_exist
from connections.app_reach import AppReach, normalize_reach
from connections.connected_accounts import (
    MICROSOFT_CAPABILITIES,
    MICROSOFT_KINDS,
    MICROSOFT_PROVIDER,
    MICROSOFT_STATES,
    Actor,
    ConnectedAccountError,
    _check_actor,
    _on_db,
    account_reaches_thread,
    credential_key,
    get_account,
    provider_accounts,
    reach_for_adding_thread,
)
from infra import key_store
from infra.database import write_transaction
from infra.time import utc_now_iso

_MICROSOFT_BUNDLE_REQUIRED = frozenset(("access_token", "refresh_token", "client_id", "tenant_id", "scopes"))


def microsoft_accounts(*, db: sqlite3.Connection) -> list[dict[str, object]]:
    """Every Microsoft account, oldest first; never credential material."""
    return provider_accounts(MICROSOFT_PROVIDER, db=db)


def create_microsoft_account(
    address: str,
    *,
    reach: object,
    created_by: Actor,
    account_kind: str,
    provider_subject: str,
    signed_in_by: Actor | None = None,
    account_id: str | None = None,
    state: str = "connected",
    db: sqlite3.Connection | None = None,
) -> dict[str, object]:
    """Create one Microsoft row identified by its issuing tenant plus object id."""
    if (
        not isinstance(address, str) or not address.strip() or state not in MICROSOFT_STATES
        or account_kind not in MICROSOFT_KINDS or not isinstance(provider_subject, str) or not provider_subject
    ):
        raise ConnectedAccountError("invalid connected account")
    _check_actor(created_by)
    if signed_in_by is not None:
        _check_actor(signed_in_by)
    normalized = reach if isinstance(reach, AppReach) else normalize_reach(reach)
    identity = account_id or str(uuid.uuid4())
    if not isinstance(identity, str) or not identity:
        raise ConnectedAccountError("invalid account id")
    now = utc_now_iso()
    with write_transaction(db, scope="microsoft_account_create") as conn:
        _assert_selected_projects_exist(normalized, db=conn)
        conn.execute(
            "INSERT INTO connected_accounts("
            "id,provider,provider_subject,account_kind,state_cause,address,capabilities_json,state,reach_mode,outside_projects,"
            "credential_key,cursor_json,created_by_kind,created_by_id,signed_in_by_kind,signed_in_by_id,created_at,updated_at"
            ") VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (identity, MICROSOFT_PROVIDER, provider_subject, account_kind, None, address.strip(),
             json.dumps(MICROSOFT_CAPABILITIES, separators=(",", ":")), state, normalized.mode,
             int(normalized.outside_projects), credential_key(identity), None, created_by[0], created_by[1],
             *(signed_in_by or (None, None)), now, now),
        )
        conn.executemany(
            "INSERT INTO connected_account_reach(account_id,project_id) VALUES (?,?)",
            ((identity, project_id) for project_id in normalized.project_ids),
        )
    return _on_db(db, lambda conn: get_account(identity, db=conn))


def create_microsoft_account_for_thread(
    address: str, *, actor_thread_id: str, created_by: Actor, db: sqlite3.Connection | None = None, **fields: object,
) -> dict[str, object]:
    """Create an account at the adding thread's server-resolved scope."""
    if "reach" in fields:
        raise ConnectedAccountError("reach comes from the adding scope")
    return _on_db(db, lambda conn: create_microsoft_account(
        address, reach=reach_for_adding_thread(actor_thread_id, db=conn), created_by=created_by, db=conn,
        **fields,  # type: ignore[arg-type]
    ))


def _microsoft_bundle(value: object) -> dict[str, object] | None:
    if not isinstance(value, dict) or set(value) != _MICROSOFT_BUNDLE_REQUIRED:
        return None
    if any(not isinstance(value[name], str) or not value[name] for name in ("access_token", "refresh_token", "client_id", "tenant_id")):
        return None
    return value if isinstance(value["scopes"], list) and all(isinstance(scope, str) for scope in value["scopes"]) else None


def _microsoft_stored_bundle(account_id: str, *, db: sqlite3.Connection) -> dict[str, object] | None:
    row = db.execute(
        "SELECT credential_key FROM connected_accounts WHERE id=? AND provider=?", (account_id, MICROSOFT_PROVIDER),
    ).fetchone()
    if row is None:
        return None
    raw = key_store.resolve_names([row["credential_key"]], db=db).get(row["credential_key"])
    try:
        return _microsoft_bundle(json.loads(raw) if raw else None)
    except (TypeError, ValueError):
        return None


def store_microsoft_credentials(
    account_id: str, credentials: dict[str, object], *, db: sqlite3.Connection | None = None,
) -> None:
    bundle = _microsoft_bundle(credentials)
    if bundle is None:
        raise ConnectedAccountError("invalid Microsoft credentials")
    with write_transaction(db, scope="microsoft_account_credentials") as conn:
        account = conn.execute(
            "SELECT credential_key FROM connected_accounts WHERE id=? AND provider=?", (account_id, MICROSOFT_PROVIDER),
        ).fetchone()
        if account is None:
            raise ConnectedAccountError("connected account not found")
        key_store.set_managed_value(account["credential_key"], json.dumps(bundle, separators=(",", ":")), db=conn)


def resolve_microsoft_credentials(account_id: str, *, db: sqlite3.Connection) -> dict[str, object] | None:
    account = get_account(account_id, db=db)
    if account["provider"] != MICROSOFT_PROVIDER or account["state"] != "connected":
        return None
    return _microsoft_stored_bundle(account_id, db=db)


def mark_microsoft_needs_sign_in(
    account_id: str, refresh_token: str | None = None, *, db: sqlite3.Connection | None = None,
) -> bool:
    with write_transaction(db, scope="microsoft_account_needs_sign_in") as conn:
        bundle = _microsoft_stored_bundle(account_id, db=conn)
        if refresh_token is not None and (bundle is None or bundle.get("refresh_token") != refresh_token):
            return False
        if conn.execute(
            "UPDATE connected_accounts SET state='needs_sign_in',state_cause=?,updated_at=? WHERE id=? AND provider=?",
            ("refresh_token_invalid", utc_now_iso(), account_id, MICROSOFT_PROVIDER),
        ).rowcount != 1:
            raise ConnectedAccountError("connected account not found")
        return True


def mark_microsoft_signed_in(
    account_id: str, *, address: str, provider_subject: str, account_kind: str, actor: Actor,
    db: sqlite3.Connection | None = None,
) -> None:
    if account_kind not in MICROSOFT_KINDS or not provider_subject:
        raise ConnectedAccountError("invalid connected account")
    _check_actor(actor)
    with write_transaction(db, scope="microsoft_account_signed_in") as conn:
        if conn.execute(
            "UPDATE connected_accounts SET state='connected',state_cause=NULL,address=?,provider_subject=?,account_kind=?,"
            "signed_in_by_kind=?,signed_in_by_id=?,updated_at=? WHERE id=? AND provider=?",
            (address.strip(), provider_subject, account_kind, actor[0], actor[1], utc_now_iso(), account_id, MICROSOFT_PROVIDER),
        ).rowcount != 1:
            raise ConnectedAccountError("connected account not found")


def _visible(account: dict[str, object], thread_id: str | None, db: sqlite3.Connection) -> bool:
    return thread_id is None or account_reaches_thread(str(account["id"]), thread_id, db=db)


def has_account(*, db: sqlite3.Connection | None = None) -> bool:
    """Whether any Microsoft row exists; later setup facts must not inspect profile prose."""
    if db is None:
        from infra.database import get_db
        connection = get_db()
        try:
            return has_account(db=connection)
        finally:
            connection.close()
    return bool(db.execute("SELECT 1 FROM connected_accounts WHERE provider=? LIMIT 1", (MICROSOFT_PROVIDER,)).fetchone())


def visible_microsoft_accounts(thread_id: str | None, *, db: sqlite3.Connection) -> list[dict[str, object]]:
    return [account for account in microsoft_accounts(db=db) if _visible(account, thread_id, db)]


def visible_microsoft_account(account_id: str, thread_id: str | None, *, db: sqlite3.Connection) -> dict[str, object] | None:
    try:
        account = get_account(account_id, db=db)
    except ConnectedAccountError:
        return None
    return account if account["provider"] == MICROSOFT_PROVIDER and _visible(account, thread_id, db) else None
