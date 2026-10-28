"""Provider-connected account records and their server-resolved reach.

One lifecycle for every provider: create, credentials and the one visibility
rule; its state, cursor and audit writes are in ``connections.account_state``.
What differs per provider is only its
:class:`~connections.account_provider.AccountProvider` entry in ``PROVIDERS``.
"""

from __future__ import annotations

import json
import logging
import sqlite3
import uuid
from typing import Literal

from connections.account_capabilities import decode_capabilities
from connections.account_provider import AccountProvider
from connections.agent_add_reach import AddingScopeError, reach_for_adding_thread as _agent_add_reach
from connections.app_lifecycle_support import _assert_selected_projects_exist
from connections.app_reach import (
    AppReach, AppReachError, normalize_reach, reach_reaches_scopes, reach_reaches_thread, resolve_thread_scopes,
)
from connections.google.provider import GOOGLE
from connections.mailhost.provider import MAILHOST
from connections.microsoft.provider import MICROSOFT
from infra import key_store
from infra.database import write_transaction
from infra.time import utc_now_iso

PROVIDERS: dict[str, AccountProvider] = {provider.name: provider for provider in (GOOGLE, MICROSOFT, MAILHOST)}
GOOGLE_PROVIDER = GOOGLE.name
MICROSOFT_PROVIDER = MICROSOFT.name
MAILHOST_PROVIDER = MAILHOST.name
ACCOUNT_STATES = frozenset(("connected", "needs_sign_in", "signed_out"))
#: Account-only reach mode: the designated CORSIA agent alone. Only the legacy migration writes it.
ONLY_CORSIA = "corsia"
Actor = tuple[Literal["person", "agent", "system"], str]
log = logging.getLogger(__name__)


class ConnectedAccountError(ValueError):
    """A stable, credential-free connected-account error."""


def provider_of(name: object) -> AccountProvider:
    """The registered provider ``name``; a row of any other provider is treated as absent."""
    provider = PROVIDERS.get(name) if isinstance(name, str) else None
    if provider is None:
        raise ConnectedAccountError("connected account not found")
    return provider


def credential_key(account_id: str, provider: str | None = None) -> str:
    """The opaque, per-account key-store name for one account's credential bundle."""
    spec = PROVIDERS.get(provider) if provider else None
    return f"{(spec.credential_prefix if spec else None) or key_store.CONNECTED_ACCOUNT_PREFIX}{account_id}"


def _is_corsia_agent_thread(thread_id: str, *, db: sqlite3.Connection) -> bool:
    """Only CORSIA: the designated CORSIA agent while available, never a project conductor; fails closed, logged."""
    from infra.corsia_agent import CorsiaAgentSettingError, is_corsia_agent
    from infra.queries import thread_available_sql

    try:
        designated = is_corsia_agent(thread_id, db=db)
    except CorsiaAgentSettingError:
        log.error("Only CORSIA accounts are unavailable: the CORSIA agent designation is unreadable")
        return False
    available = f"SELECT 1 FROM threads WHERE id=? AND {thread_available_sql()}"
    return designated and db.execute(available, (thread_id,)).fetchone() is not None


def _account_reach(account_id: str, *, db: sqlite3.Connection) -> AppReach:
    row = db.execute(
        "SELECT reach_mode,outside_projects FROM connected_accounts WHERE id=?", (account_id,),
    ).fetchone()
    if row is None:
        raise ConnectedAccountError("connected account not found")
    project_ids = frozenset(
        row["project_id"]
        for row in db.execute(
            "SELECT project_id FROM connected_account_reach WHERE account_id=?", (account_id,),
        )
    )
    return AppReach(mode=row["reach_mode"], project_ids=project_ids, outside_projects=bool(row["outside_projects"]))


def account_reaches_thread(account_id: str, thread_id: str, *, db: sqlite3.Connection) -> bool:
    """The one reach check every agent surface asks: can this account serve the caller's full parent chain."""
    try:
        reach = _account_reach(account_id, db=db)
        if reach.mode == ONLY_CORSIA:
            return _is_corsia_agent_thread(thread_id, db=db)
        return reach_reaches_thread(reach, thread_id, db=db)
    except (AppReachError, ConnectedAccountError):
        return False


def capabilities_reaching_thread(thread_id: str, *, db: sqlite3.Connection) -> frozenset[str]:
    """Union capabilities of accounts reaching a thread, resolving the parent chain once."""
    rows = db.execute("SELECT id,reach_mode,outside_projects,capabilities_json FROM connected_accounts").fetchall()
    projects: dict[str, set[str]] = {}
    for row in db.execute("SELECT account_id,project_id FROM connected_account_reach"):
        projects.setdefault(row["account_id"], set()).add(row["project_id"])
    scopes = resolve_thread_scopes(thread_id, db=db)
    corsia = any(row["reach_mode"] == ONLY_CORSIA for row in rows) and _is_corsia_agent_thread(thread_id, db=db)
    result: set[str] = set()
    for row in rows:
        capabilities = decode_capabilities(row["capabilities_json"])
        if capabilities is None:
            continue
        if row["reach_mode"] == ONLY_CORSIA:
            reached = corsia
        else:
            reach = AppReach(row["reach_mode"], frozenset(projects.get(row["id"], set())), bool(row["outside_projects"]))
            reached = reach_reaches_scopes(reach, scopes)
        if reached:
            result.update(capabilities)
    return frozenset(result)


def get_account(account_id: str, *, db: sqlite3.Connection) -> dict[str, object]:
    """Return one account's non-credential facts and reach."""
    row = db.execute("SELECT * FROM connected_accounts WHERE id=?", (account_id,)).fetchone()
    if row is None:
        raise ConnectedAccountError("connected account not found")
    try:
        capabilities = json.loads(row["capabilities_json"])
        cursor = json.loads(row["cursor_json"]) if row["cursor_json"] else None
    except (TypeError, ValueError):
        raise ConnectedAccountError("invalid connected account") from None
    if not isinstance(capabilities, list) or not all(isinstance(item, str) for item in capabilities):
        raise ConnectedAccountError("invalid connected account")
    return {
        "id": row["id"], "provider": row["provider"], "provider_subject": row["provider_subject"],
        "account_kind": row["account_kind"], "state_cause": row["state_cause"],
        "address": row["address"], "capabilities": capabilities, "state": row["state"],
        "reach": _account_reach(account_id, db=db).as_dict(), "credential_key": row["credential_key"],
        "cursor": cursor, "created_by": (row["created_by_kind"], row["created_by_id"]),
        "signed_in_by": (row["signed_in_by_kind"], row["signed_in_by_id"])
        if row["signed_in_by_kind"] is not None else None,
        "created_at": row["created_at"], "updated_at": row["updated_at"],
    }


def list_accounts(provider: str | None = None, *, db: sqlite3.Connection) -> list[dict[str, object]]:
    """Every account of one provider (``None``: every registered provider), oldest first; never credential material."""
    names = [provider] if provider is not None else sorted(PROVIDERS)
    rows = db.execute(
        f"SELECT id FROM connected_accounts WHERE provider IN ({','.join('?' * len(names))}) ORDER BY created_at,id",
        names,
    ).fetchall()
    return [get_account(row["id"], db=db) for row in rows]


def _visible(account: dict[str, object], thread_id: str | None, db: sqlite3.Connection) -> bool:
    """The one visibility rule: the person sees every account; an agent, only those that reach its thread."""
    return thread_id is None or account_reaches_thread(str(account["id"]), thread_id, db=db)


def visible_accounts(
    thread_id: str | None, *, db: sqlite3.Connection, provider: str | None = None,
) -> list[dict[str, object]]:
    """Every account this caller may see (``thread_id`` None is the person), of one provider or all."""
    return [account for account in list_accounts(provider, db=db) if _visible(account, thread_id, db)]


def visible_account(
    account_id: str, thread_id: str | None, *, db: sqlite3.Connection, provider: str | None = None,
) -> dict[str, object] | None:
    """The account ``account_id`` names if this caller may see it (any, for the person); None alike
    for an unknown id, an out-of-reach one and another provider's, so no surface can tell them apart."""
    try:
        account = get_account(account_id, db=db)
    except ConnectedAccountError:
        return None
    if account["provider"] not in PROVIDERS or (provider is not None and account["provider"] != provider):
        return None
    return account if _visible(account, thread_id, db) else None


def is_connected(account_id: str, *, db: sqlite3.Connection) -> bool:
    """Whether the account still exists and is connected: the gate before any write for it."""
    return db.execute(
        "SELECT 1 FROM connected_accounts WHERE id=? AND state='connected'", (account_id,),
    ).fetchone() is not None


def reach_for_adding_thread(thread_id: str, *, db: sqlite3.Connection) -> AppReach:
    """Derive a new app or account's reach from its server-known adding scope."""
    try:
        return _agent_add_reach(thread_id, db=db)
    except AddingScopeError:
        raise ConnectedAccountError("adding scope is unavailable") from None


def create_account_for_thread(
    provider: str, address: str, *, actor_thread_id: str, created_by: Actor, db: sqlite3.Connection | None = None,
    **fields: object,
) -> dict[str, object]:
    """Create an account at the caller's scope; no request can widen its reach.

    ``fields`` are :func:`create_account`'s other keywords, never ``reach``.
    """
    if "reach" in fields:
        raise ConnectedAccountError("reach comes from the adding scope")
    return _on_db(db, lambda conn: create_account(
        provider, address, reach=reach_for_adding_thread(actor_thread_id, db=conn), created_by=created_by, db=conn,
        **fields,  # type: ignore[arg-type]
    ))


def _on_db(db: sqlite3.Connection | None, read):  # type: ignore[no-untyped-def]
    """Run ``read`` on the caller's connection, or on a new one this frame closes."""
    if db is not None:
        return read(db)
    from infra.database import get_db

    connection = get_db()
    try:
        return read(connection)
    finally:
        connection.close()


def _check_actor(actor: object) -> None:
    if not isinstance(actor, tuple) or len(actor) != 2 or actor[0] not in {"person", "agent", "system"} or not actor[1]:
        raise ConnectedAccountError("invalid account actor")


def _check_identity(spec: AccountProvider, provider_subject: object, account_kind: object) -> None:
    if provider_subject is not None and (not isinstance(provider_subject, str) or not provider_subject):
        raise ConnectedAccountError("invalid provider subject")
    if spec.subject_required and provider_subject is None:
        raise ConnectedAccountError("invalid provider subject")
    if (account_kind not in spec.account_kinds) if spec.account_kinds else account_kind is not None:
        raise ConnectedAccountError("invalid account kind")


def create_account(
    provider: str,
    address: str,
    *,
    reach: object,
    created_by: Actor,
    signed_in_by: Actor | None = None,
    provider_subject: str | None = None,
    account_kind: str | None = None,
    cursor: dict[str, object] | None = None,
    account_id: str | None = None,
    state: str = "connected",
    db: sqlite3.Connection | None = None,
) -> dict[str, object]:
    """Create one provider's row using the shared reach representation.

    Credentials are stored separately via :func:`store_credentials` so a
    caller can put both rows inside one atomic transaction.
    """
    spec = provider_of(provider)
    if not isinstance(address, str) or not address.strip() or state not in ACCOUNT_STATES:
        raise ConnectedAccountError("invalid connected account")
    _check_identity(spec, provider_subject, account_kind)
    _check_actor(created_by)
    if signed_in_by is not None:
        _check_actor(signed_in_by)
    normalized = reach if isinstance(reach, AppReach) else normalize_reach(reach)
    identity = account_id or str(uuid.uuid4())
    if not isinstance(identity, str) or not identity:
        raise ConnectedAccountError("invalid account id")
    cursor_json = json.dumps(cursor, separators=(",", ":"), sort_keys=True) if cursor is not None else None
    now = utc_now_iso()
    with write_transaction(db, scope="connected_account_create") as conn:
        _assert_selected_projects_exist(normalized, db=conn)
        conn.execute(
            "INSERT INTO connected_accounts("
            "id,provider,provider_subject,account_kind,address,capabilities_json,state,reach_mode,outside_projects,"
            "credential_key,cursor_json,created_by_kind,created_by_id,signed_in_by_kind,signed_in_by_id,"
            "created_at,updated_at"
            ") VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (identity, spec.name, provider_subject, account_kind, address.strip(),
             json.dumps(spec.capabilities, separators=(",", ":")), state, normalized.mode,
             int(normalized.outside_projects), credential_key(identity, spec.name), cursor_json,
             created_by[0], created_by[1], *(signed_in_by or (None, None)), now, now),
        )
        conn.executemany(
            "INSERT INTO connected_account_reach(account_id,project_id) VALUES (?,?)",
            ((identity, project_id) for project_id in normalized.project_ids),
        )
    return _on_db(db, lambda conn: get_account(identity, db=conn))


def backfill_subject(account_id: str, provider_subject: str, *, db: sqlite3.Connection | None = None) -> None:
    """Persist a provider subject only for a migrated row that lacks one."""
    if not isinstance(provider_subject, str) or not provider_subject:
        raise ConnectedAccountError("invalid provider subject")
    with write_transaction(db, scope="connected_account_subject") as conn:
        if conn.execute("SELECT 1 FROM connected_accounts WHERE id=?", (account_id,)).fetchone() is None:
            raise ConnectedAccountError("connected account not found")
        conn.execute(
            "UPDATE connected_accounts SET provider_subject=?,updated_at=? WHERE id=? AND provider_subject IS NULL",
            (provider_subject, utc_now_iso(), account_id),
        )


def _credential_row(account_id: str, db: sqlite3.Connection) -> tuple[AccountProvider, str] | None:
    row = db.execute("SELECT provider,credential_key FROM connected_accounts WHERE id=?", (account_id,)).fetchone()
    if row is None or row["provider"] not in PROVIDERS:
        return None
    return PROVIDERS[row["provider"]], row["credential_key"]


def _stored_bundle(account_id: str, *, db: sqlite3.Connection) -> tuple[AccountProvider, dict] | None:
    found = _credential_row(account_id, db)
    if found is None:
        return None
    spec, key = found
    stored = key_store.resolve_names([key], db=db).get(key)
    if spec.raw_credential_field is not None:
        return (spec, {spec.raw_credential_field: stored}) if stored else None
    try:
        bundle = json.loads(stored) if stored else None
    except (TypeError, ValueError):
        return None
    return (spec, bundle) if isinstance(bundle, dict) else None


def _write_bundle(account_id: str, bundle: dict[str, object], *, db: sqlite3.Connection | None) -> None:
    with write_transaction(db, scope="connected_account_credentials") as conn:
        found = _credential_row(account_id, conn)
        if found is None:
            raise ConnectedAccountError("connected account not found")
        raw = found[0].raw_credential_field
        if raw is None:
            key_store.set_managed_value(found[1], json.dumps(bundle, separators=(",", ":")), db=conn)
        elif bundle.get(raw):
            key_store.set_managed_value(found[1], bundle[raw], db=conn)
        else:
            key_store.clear_value(found[1], db=conn)


def store_credentials(account_id: str, credentials: dict[str, object], *, db: sqlite3.Connection | None = None) -> None:
    """Store one complete credential bundle, in its provider's shape, against an existing account."""
    with write_transaction(db, scope="connected_account_credentials") as conn:
        found = _credential_row(account_id, conn)
        if found is None:
            raise ConnectedAccountError("connected account not found")
        if not found[0].valid_bundle(credentials):
            raise ConnectedAccountError(f"invalid {found[0].display_name} credentials")
        _write_bundle(account_id, credentials, db=conn)


def resolve_credentials(account_id: str, *, db: sqlite3.Connection) -> dict[str, object] | None:
    """Return one account's usable bundle (its provider's required fields) or None, never another account's."""
    stored = _stored_bundle(account_id, db=db)
    if stored is None or not stored[0].valid_bundle(stored[1]):
        return None
    spec, bundle = stored
    keys = spec.required_fields | (set(bundle) & spec.optional_bundle_fields)
    return {key: bundle[key] for key in keys}


def account_client(account_id: str, *, db: sqlite3.Connection) -> dict[str, str] | None:
    """The account's saved registration (its provider's client fields), kept after Sign out so
    signing in again needs no new setup."""
    stored = _stored_bundle(account_id, db=db)
    if stored is None or not stored[0].valid_bundle(stored[1], stored[0].client_fields):
        return None
    spec, bundle = stored
    return {key: bundle[key] for key in spec.client_fields}


def account_registration(account_id: str, *, db: sqlite3.Connection) -> dict[str, str | None]:
    """The registration GUIDs one account's row may show (``client_id``, ``tenant_id``), each None
    unless its provider keeps a per-account registration; never any other bundle field."""
    found = _credential_row(account_id, db)
    client = account_client(account_id, db=db) if found is not None and found[0].registration is not None else None
    client_id, tenant_id = found[0].registration(client) if found is not None and client is not None else (None, None)
    return {"client_id": client_id, "tenant_id": tenant_id}


def take_refresh_token(account_id: str, *, db: sqlite3.Connection | None = None) -> dict[str, object] | None:
    """Strip one bundle to its client fields, returning the usable bundle it held for the revoke only.

    The client stays so the row can sign in again; the row resolves no usable credential.
    """
    with write_transaction(db, scope="connected_account_take_token") as conn:
        stored = _stored_bundle(account_id, db=conn)
        if stored is None or not stored[0].valid_bundle(stored[1]):
            return None
        spec, bundle = stored
        _write_bundle(account_id, {key: bundle[key] for key in spec.client_fields}, db=conn)
        return {key: bundle[key] for key in spec.required_fields}


def granted_scopes(account_id: str, *, db: sqlite3.Connection) -> list[str]:
    """Return one account's saved granted scopes without exposing credentials."""
    stored = _stored_bundle(account_id, db=db)
    scopes = stored[1].get("scopes") if stored is not None and stored[0].valid_bundle(stored[1]) else None
    return sorted(scope for scope in scopes if isinstance(scope, str)) if isinstance(scopes, list) else []
