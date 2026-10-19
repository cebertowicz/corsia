"""Provider-connected account records and their server-resolved reach."""

from __future__ import annotations

import json
import sqlite3
import uuid
from typing import Literal

from connections.app_lifecycle_support import _assert_selected_projects_exist
from connections.app_reach import AppReach, AppReachError, normalize_reach, reach_reaches_thread
from infra import key_store
from infra.database import write_transaction
from infra.time import utc_now_iso

GOOGLE_PROVIDER = "google"
GOOGLE_CAPABILITIES = ("mail", "calendar", "files")
GOOGLE_STATES = frozenset(("connected", "needs_sign_in", "signing_in", "signed_out", "removing"))
AUDIT_ACTIONS = frozenset(("connect", "sign_in", "sign_out", "remove"))
REVOCATION_CAUSES = frozenset((
    "revoked", "provider_unreachable", "provider_timeout", "provider_rejected",
    "provider_unexpected", "manual_only",
))
Actor = tuple[Literal["person", "agent", "system"], str]


class ConnectedAccountError(ValueError):
    """A stable, credential-free connected-account error."""


def credential_key(account_id: str) -> str:
    """The opaque, per-account key-store name for a Google credential bundle."""
    return f"connected-account--google--{account_id}"


def corsia_only_reach() -> AppReach:
    """Today's designated CORSIA scope in the shared hosted-app reach model."""
    return AppReach(mode="selected", project_ids=frozenset(), outside_projects=True)


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
    return AppReach(
        mode=row["reach_mode"], project_ids=project_ids,
        outside_projects=bool(row["outside_projects"]),
    )


def account_reaches_thread(account_id: str, thread_id: str, *, db: sqlite3.Connection) -> bool:
    """Whether an account can serve the caller's full parent chain, failing closed."""
    try:
        return reach_reaches_thread(_account_reach(account_id, db=db), thread_id, db=db)
    except (AppReachError, ConnectedAccountError):
        return False


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
        "address": row["address"], "capabilities": capabilities, "state": row["state"],
        "reach": _account_reach(account_id, db=db).as_dict(), "credential_key": row["credential_key"],
        "cursor": cursor, "created_by": (row["created_by_kind"], row["created_by_id"]),
        "signed_in_by": (
            (row["signed_in_by_kind"], row["signed_in_by_id"])
            if row["signed_in_by_kind"] is not None else None
        ),
        "created_at": row["created_at"], "updated_at": row["updated_at"],
    }


def google_account(*, db: sqlite3.Connection) -> dict[str, object] | None:
    """The temporary single-account compatibility accessor for PR A consumers."""
    row = db.execute(
        "SELECT id FROM connected_accounts WHERE provider=? ORDER BY created_at,id LIMIT 1",
        (GOOGLE_PROVIDER,),
    ).fetchone()
    return get_account(row["id"], db=db) if row is not None else None


def reach_for_adding_thread(thread_id: str, *, db: sqlite3.Connection) -> AppReach:
    """Derive a new account's reach from its server-known adding scope."""
    from connections.account_policy import missing_project_reason, thread_scope

    scope = thread_scope(thread_id, db=db)
    if missing_project_reason(scope, db=db):
        raise ConnectedAccountError("adding scope is unavailable")
    project_id = scope["project_id"]
    return AppReach(
        mode="all" if project_id is None else "selected",
        project_ids=frozenset() if project_id is None else frozenset((str(project_id),)),
        outside_projects=False,
    )


def create_google_account_for_thread(
    address: str, *, actor_thread_id: str, created_by: Actor, db: sqlite3.Connection | None = None,
) -> dict[str, object]:
    """Create an account at the caller's scope; no request can widen its reach."""
    if db is None:
        from infra.database import get_db

        connection = get_db()
        try:
            return create_google_account(
                address, reach=reach_for_adding_thread(actor_thread_id, db=connection),
                created_by=created_by, db=connection,
            )
        finally:
            connection.close()
    return create_google_account(
        address, reach=reach_for_adding_thread(actor_thread_id, db=db), created_by=created_by, db=db,
    )


def create_google_account(
    address: str,
    *,
    reach: object,
    created_by: Actor,
    signed_in_by: Actor | None = None,
    provider_subject: str | None = None,
    cursor: dict[str, object] | None = None,
    account_id: str | None = None,
    state: str = "connected",
    db: sqlite3.Connection | None = None,
) -> dict[str, object]:
    """Create a Google row using the shared reach representation.

    Credentials are stored separately via :func:`store_google_credentials` so a
    caller can put both rows inside one atomic migration transaction.
    """
    if not isinstance(address, str) or not address.strip() or state not in GOOGLE_STATES:
        raise ConnectedAccountError("invalid connected account")
    if provider_subject is not None and (not isinstance(provider_subject, str) or not provider_subject):
        raise ConnectedAccountError("invalid provider subject")
    if not isinstance(created_by, tuple) or len(created_by) != 2 or created_by[0] not in {"person", "agent", "system"} or not created_by[1]:
        raise ConnectedAccountError("invalid account actor")
    if signed_in_by is not None and (
        not isinstance(signed_in_by, tuple) or len(signed_in_by) != 2
        or signed_in_by[0] not in {"person", "agent", "system"} or not signed_in_by[1]
    ):
        raise ConnectedAccountError("invalid account actor")
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
            "id,provider,provider_subject,address,capabilities_json,state,reach_mode,outside_projects,"
            "credential_key,cursor_json,created_by_kind,created_by_id,signed_in_by_kind,signed_in_by_id,"
            "created_at,updated_at"
            ") VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (identity, GOOGLE_PROVIDER, provider_subject, address.strip(),
             json.dumps(GOOGLE_CAPABILITIES, separators=(",", ":")), state, normalized.mode,
             int(normalized.outside_projects), credential_key(identity), cursor_json,
             created_by[0], created_by[1], *(signed_in_by or (None, None)), now, now),
        )
        conn.executemany(
            "INSERT INTO connected_account_reach(account_id,project_id) VALUES (?,?)",
            ((identity, project_id) for project_id in normalized.project_ids),
        )
    if db is not None:
        return get_account(identity, db=db)
    from infra.database import get_db

    connection = get_db()
    try:
        return get_account(identity, db=connection)
    finally:
        connection.close()


def backfill_google_subject(
    account_id: str, provider_subject: str, *, db: sqlite3.Connection | None = None,
) -> None:
    """Persist a Google subject only for a migrated row that lacks one."""
    if not isinstance(provider_subject, str) or not provider_subject:
        raise ConnectedAccountError("invalid provider subject")
    with write_transaction(db, scope="connected_account_subject") as conn:
        row = conn.execute(
            "SELECT provider_subject FROM connected_accounts WHERE id=? AND provider=?",
            (account_id, GOOGLE_PROVIDER),
        ).fetchone()
        if row is None:
            raise ConnectedAccountError("connected account not found")
        if row["provider_subject"] is None:
            conn.execute(
                "UPDATE connected_accounts SET provider_subject=?,updated_at=? WHERE id=?",
                (provider_subject, utc_now_iso(), account_id),
            )


def store_google_credentials(
    account_id: str, credentials: dict[str, object], *, db: sqlite3.Connection | None = None,
) -> None:
    """Store one complete Google credential bundle against an existing account."""
    required = {"client_id", "client_secret", "refresh_token"}
    keys = set(credentials)
    if (
        not required <= keys or keys - (required | {"scopes"})
        or any(not isinstance(credentials[key], str) or not credentials[key] for key in required)
        or ("scopes" in credentials and (
            not isinstance(credentials["scopes"], list)
            or any(not isinstance(scope, str) for scope in credentials["scopes"])
        ))
    ):
        raise ConnectedAccountError("invalid Google credentials")
    with write_transaction(db, scope="connected_account_credentials") as conn:
        account = conn.execute(
            "SELECT credential_key FROM connected_accounts WHERE id=? AND provider=?", (account_id, GOOGLE_PROVIDER),
        ).fetchone()
        if account is None:
            raise ConnectedAccountError("connected account not found")
        key_store.set_managed_value(account["credential_key"], json.dumps(credentials, separators=(",", ":")), db=conn)


def resolve_google_credentials(account_id: str, *, db: sqlite3.Connection) -> dict[str, str] | None:
    """Return one account's complete Google bundle or None, never another account's."""
    row = db.execute(
        "SELECT credential_key FROM connected_accounts WHERE id=? AND provider=?", (account_id, GOOGLE_PROVIDER),
    ).fetchone()
    if row is None:
        return None
    stored = key_store.resolve_names([row["credential_key"]], db=db).get(row["credential_key"])
    if stored is None:
        return None
    try:
        bundle = json.loads(stored)
    except (TypeError, ValueError):
        return None
    required = {"client_id", "client_secret", "refresh_token"}
    if (
        not isinstance(bundle, dict) or not required <= set(bundle)
        or set(bundle) - (required | {"scopes"})
        or any(not isinstance(bundle[key], str) or not bundle[key] for key in required)
        or ("scopes" in bundle and (
            not isinstance(bundle["scopes"], list)
            or any(not isinstance(scope, str) for scope in bundle["scopes"])
        ))
    ):
        return None
    return {key: bundle[key] for key in required}


def google_scopes(account_id: str, *, db: sqlite3.Connection) -> list[str]:
    """Return one account's saved granted OAuth scopes without exposing credentials."""
    row = db.execute("SELECT credential_key FROM connected_accounts WHERE id=?", (account_id,)).fetchone()
    if row is None:
        return []
    stored = key_store.resolve_names([row["credential_key"]], db=db).get(row["credential_key"])
    try:
        bundle = json.loads(stored) if stored else {}
    except (TypeError, ValueError):
        return []
    scopes = bundle.get("scopes") if isinstance(bundle, dict) else None
    return sorted(scope for scope in scopes if isinstance(scope, str)) if isinstance(scopes, list) else []


def set_state(account_id: str, state: str, *, db: sqlite3.Connection | None = None) -> None:
    """Update one account's lifecycle state; callers own the provider operation."""
    if state not in GOOGLE_STATES:
        raise ConnectedAccountError("invalid connected account state")
    with write_transaction(db, scope="connected_account_state") as conn:
        if conn.execute(
            "UPDATE connected_accounts SET state=?,updated_at=? WHERE id=?", (state, utc_now_iso(), account_id),
        ).rowcount != 1:
            raise ConnectedAccountError("connected account not found")


def set_cursor(account_id: str, cursor: dict[str, object] | None, *, db: sqlite3.Connection | None = None) -> None:
    """Persist one provider cursor on its own account row."""
    encoded = json.dumps(cursor, separators=(",", ":"), sort_keys=True) if cursor is not None else None
    with write_transaction(db, scope="connected_account_cursor") as conn:
        if conn.execute(
            "UPDATE connected_accounts SET cursor_json=?,updated_at=? WHERE id=?", (encoded, utc_now_iso(), account_id),
        ).rowcount != 1:
            raise ConnectedAccountError("connected account not found")


def record_audit(
    account_id: str, action: str, actor: Actor, *, revocation_cause: str | None = None,
    db: sqlite3.Connection | None = None,
) -> None:
    """Record a fixed account lifecycle action without provider response text."""
    if action not in AUDIT_ACTIONS or actor[0] not in {"person", "agent", "system"} or not actor[1]:
        raise ConnectedAccountError("invalid account audit")
    if revocation_cause is not None and revocation_cause not in REVOCATION_CAUSES:
        raise ConnectedAccountError("invalid account audit")
    with write_transaction(db, scope="connected_account_audit") as conn:
        if conn.execute("SELECT 1 FROM connected_accounts WHERE id=?", (account_id,)).fetchone() is None:
            raise ConnectedAccountError("connected account not found")
        conn.execute(
            "INSERT INTO connected_account_audit(created_at,account_id,actor_kind,actor_id,action,revocation_cause) "
            "VALUES (?,?,?,?,?,?)",
            (utc_now_iso(), account_id, actor[0], actor[1], action, revocation_cause),
        )
