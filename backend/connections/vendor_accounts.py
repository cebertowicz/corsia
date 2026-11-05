"""Durable vendor identities and home references; credentials stay vendor-owned."""
from __future__ import annotations

import json
import uuid
from pathlib import Path
from typing import Any

from connections import fact_memo
from connections.account_row_shape import COLUMNS, JOIN, project_row
from infra.database import get_db, write_transaction
from infra.time import utc_now_iso

VENDORS = ("claude-code", "codex")
# The name the built-in account is seeded with; account_label treats it as "never renamed".
# migration_80_to_81_vendor_accounts keeps its own literal (a migration must not import moving code).
DEFAULT_ACCOUNT_NAME = "Default"
# The only causes a cleanup receipt may carry: fixed names from code, never provider text.
AUTH_CLEANUP_CAUSES = frozenset({
    None, "claude_executable_missing", "codex_executable_missing", "github_executable_missing", "vendor_logout_failed",
    "vendor_logout_unavailable", "sign_out_result_invalid", "sign_out_hook_failed", "signin_stop_failed", "no_home"})


class AccountError(ValueError):
    """A requested identity or mutation is not valid.

    ``code`` is the stable machine-readable cause the API delivers beside the
    message, so clients never branch on wording.
    """

    code = "account_conflict"


def require_vendor(provider: str) -> None:
    """Accept only a built-in vendor: project policy, binding and launch."""
    if provider not in VENDORS:
        raise AccountError("provider must be claude-code or codex")


def require_account_provider(provider: str) -> None:
    """Accept any provider with a registered profile: account rows only."""
    from connections.account_profiles import account_providers

    if provider not in account_providers():
        raise AccountError("account provider is not registered")


@fact_memo.memoized_reader
def account_selection_facts(account_id: str, *, db=None) -> dict[str, Any]:
    """Read the non-private facts needed for policy selection and denial."""
    connection = db or get_db()
    try:
        row = connection.execute(
            "SELECT a.id,a.state,a.is_default,c.provider" + JOIN + " WHERE a.id=?", (account_id,)
        ).fetchone()
        if row is None:
            raise AccountError("account does not exist")
        return dict(row)
    finally:
        if db is None:
            connection.close()


def _account_rows(columns: str, *, provider: str | None, include_removed: bool, db):
    """The one account filter and provider selection order; callers choose only the columns."""
    clauses, params = [], []
    if provider is not None:
        require_account_provider(provider)
        clauses.append("c.provider=?")
        params.append(provider)
    if not include_removed:
        clauses.append("a.state!='removed'")
    where = " WHERE " + " AND ".join(clauses) if clauses else ""
    return db.execute("SELECT " + columns + JOIN + where + " ORDER BY a.is_default DESC,a.created_at,a.id", params)


@fact_memo.memoized_reader
def list_account_selection_facts(provider: str, *, db) -> list[dict[str, Any]]:
    """Read non-private account facts in the canonical provider selection order."""
    return [dict(row) for row in _account_rows("a.id,a.state,a.is_default,c.provider",
                                               provider=provider, include_removed=False, db=db)]


@fact_memo.memoized_reader
def get_account(account_id: str, *, db=None) -> dict[str, Any]:
    """Read metadata including retained tombstones, without reading usage."""
    connection = db or get_db()
    try:
        row = connection.execute("SELECT " + COLUMNS + JOIN + " WHERE a.id=?", (account_id,)).fetchone()
        if row is None:
            raise AccountError("account does not exist")
        return project_row(row)
    finally:
        if db is None:
            connection.close()



@fact_memo.memoized_reader
def resolve_account_id(provider: str, account_id: str | None = None, *, db) -> str | None:
    """Resolve one explicit or workspace default identity, never a substitute."""
    require_account_provider(provider)
    clause = "a.id=?" if account_id is not None else "a.is_default=1 AND a.state!='removed'"
    params = (provider, account_id) if account_id is not None else (provider,)
    row = db.execute("SELECT a.id" + JOIN + " WHERE c.provider=? AND " + clause, params).fetchone()
    if row is None and account_id is not None:
        raise AccountError("account does not exist for this provider")
    return str(row["id"]) if row else None


def default_account(provider: str, *, db=None) -> dict[str, Any]:
    connection = db or get_db()
    try:
        identity = resolve_account_id(provider, db=connection)
        if identity is None:
            raise AccountError("vendor default account is missing")
        return get_account(identity, db=connection)
    finally:
        if db is None:
            connection.close()


@fact_memo.memoized_reader
def list_accounts(*, provider: str | None = None, include_removed: bool = False, db=None) -> list[dict[str, Any]]:
    connection = db or get_db()
    try:
        rows = _account_rows(COLUMNS, provider=provider, include_removed=include_removed, db=connection)
        return [project_row(row) for row in rows]
    finally:
        if db is None:
            connection.close()


def comparison_facts(account: dict) -> tuple[str, ...] | None:
    """The complete comparison key for this vendor, or no claim."""
    from connections.account_profiles import profile_for

    identity = account["identity"]
    keys = profile_for(account["provider"]).comparison_keys
    values = tuple(identity.get(key) for key in keys)
    return values if values and all(isinstance(value, str) and value for value in values) else None


def same_account(left: dict, right: dict) -> bool:
    """Compare one vendor's complete stored facts."""
    facts = comparison_facts(left)
    return left["provider"] == right["provider"] and facts is not None and facts == comparison_facts(right)


def duplicate_of(account: dict, *, rows: list[dict] | None = None, db=None) -> str | None:
    """The oldest other non-removed row matching this vendor's stored comparison facts."""
    if account["state"] == "removed":
        return None
    if rows is None:
        rows = list_accounts(provider=account["provider"], db=db)
    for other in sorted(rows, key=lambda row: (row["created_at"], row["id"])):
        if other["id"] != account["id"] and other["state"] != "removed" and same_account(account, other):
            return str(other["id"])
    return None


def _name(name: str) -> str:
    if not isinstance(name, str) or not name.strip():
        raise AccountError("account name must be non-empty")
    return name.strip()


def _hint(profile, identity_hint: dict | None) -> dict | None:
    if identity_hint is None:
        return None
    if profile.validate_hint is None:
        raise AccountError("this provider takes no identity hint")
    hint = profile.validate_hint(identity_hint)
    if not isinstance(hint, dict):
        raise AccountError("identity hint must be an object")
    return hint


def allocate_account_homes(provider: str, identity: str):
    """Allocate the private homes of a new account; ``add_account`` is the only caller."""
    from connections.account_profiles import profile_for

    root = profile_for(provider).home_root() / identity
    source, runtime = root / 'source', root / 'runtime'
    for directory in (root.parent, root, source, runtime):
        directory.mkdir(parents=True, mode=0o700, exist_ok=True)
        directory.chmod(0o700)
    return root, source, runtime


def add_account(provider: str, name: str, *, identity_hint: dict | None = None, db=None) -> dict[str, Any]:
    """Create a signed-out identity with two generated private distinct homes.

    A provider's validated ``identity_hint`` is kept under ``identity["hint"]``,
    apart from the facts the provider itself reports.
    """
    from connections.account_profiles import profile_for

    profile = profile_for(provider)
    name = _name(name)
    hint = _hint(profile, identity_hint)
    identity = uuid.uuid4().hex
    with write_transaction(db, scope="add_vendor_account") as connection:
        transport = connection.execute("SELECT id FROM connections WHERE provider=?", (provider,)).fetchone()
        if transport is None:
            raise AccountError("vendor connection is not configured")
        # Checked inside this immediate transaction, so two adds cannot both pass.
        if profile.single_account and connection.execute(
                "SELECT 1 FROM vendor_accounts WHERE connection_id=? AND state!='removed'",
                (transport["id"],)).fetchone() is not None:
            raise AccountError(f"{profile.label} already exists")
        root, source, runtime = allocate_account_homes(provider, identity)
        now = utc_now_iso()
        try:
            connection.execute("""INSERT INTO vendor_accounts
                (id,connection_id,name,source_home,runtime_home,state,identity_json,created_at,updated_at)
                VALUES (?,?,?,?,?,'signed_out',?,?,?)""",
                (identity, transport["id"], name, str(source), str(runtime),
                 json.dumps({"hint": hint} if hint is not None else {}, sort_keys=True), now, now))
        except BaseException:
            # Only empty homes from this operation; never credential cleanup.
            runtime.rmdir()
            source.rmdir()
            root.rmdir()
            raise
        return get_account(identity, db=connection)


def _current(account_id: str, expected_revision: int, db) -> dict:
    account = get_account(account_id, db=db)
    if account["revision"] != expected_revision:
        raise AccountError("account revision changed; refresh before acting")
    if account["state"] == "removed":
        raise AccountError("account was removed; choose another permitted account")
    return account


def rename_account(account_id: str, name: str, *, expected_revision: int, db=None) -> dict[str, Any]:
    name = _name(name)
    with write_transaction(db, scope="rename_vendor_account") as connection:
        _current(account_id, expected_revision, connection)
        connection.execute("UPDATE vendor_accounts SET name=?,revision=revision+1,updated_at=? WHERE id=?",
                           (name, utc_now_iso(), account_id))
        return get_account(account_id, db=connection)


def _write_default(account: dict, *, db) -> None:
    """Set one workspace Default, clearing any prior marker including tombstones."""
    now = utc_now_iso()
    db.execute("UPDATE vendor_accounts SET is_default=0,revision=revision+1,updated_at=? "
               "WHERE connection_id=? AND is_default=1 AND id!=?", (now, account['connection_id'], account['id']))
    db.execute("UPDATE vendor_accounts SET is_default=1,revision=revision+1,updated_at=? WHERE id=?",
               (now, account['id']))


def default_on_first_signin(account: dict, *, db) -> None:
    """Make a first signed-in vendor account Default if no live Default exists."""
    if account['provider'] in VENDORS and resolve_account_id(account['provider'], db=db) is None:
        _write_default(account, db=db)


def set_default_account(account_id: str, *, expected_revision: int, db=None) -> dict[str, Any]:
    """Make this account its provider's workspace default, where the provider allows choosing one."""
    from connections.account_profiles import profile_for

    with write_transaction(db, scope="vendor_account_default") as connection:
        account = _current(account_id, expected_revision, connection)
        if not profile_for(account["provider"]).workspace_default:
            raise AccountError("this provider's workspace default cannot be changed")
        _write_default(account, db=connection)
        return get_account(account_id, db=connection)


def clear_default_account(account_id: str, *, expected_revision: int, db=None) -> dict[str, Any]:
    """Leave this provider with no workspace default; a signed-out or removed default may be cleared."""
    from connections.account_profiles import profile_for

    with write_transaction(db, scope="vendor_account_default") as connection:
        account = get_account(account_id, db=connection)
        if account["revision"] != expected_revision:
            raise AccountError("account revision changed; refresh before acting")
        if not profile_for(account["provider"]).workspace_default:
            raise AccountError("this provider's workspace default cannot be changed")
        if not account["is_default"]:
            raise AccountError("account is not the workspace default")
        connection.execute("UPDATE vendor_accounts SET is_default=0,revision=revision+1,updated_at=? WHERE id=?",
                           (utc_now_iso(), account_id))
        return get_account(account_id, db=connection)


def set_account_state(account_id: str, state: str, *, expected_revision: int, db=None) -> dict[str, Any]:
    """Commit explicit lifecycle truth; removal is terminal and retains bindings."""
    if state not in ("signed_in", "signed_out", "removed"):
        raise AccountError("invalid account state")
    with write_transaction(db, scope="vendor_account_lifecycle") as connection:
        account = _current(account_id, expected_revision, connection)
        now = utc_now_iso()
        connection.execute("""UPDATE vendor_accounts SET state=?,revision=revision+1,updated_at=?,
            removed_at=CASE WHEN ?='removed' THEN ? ELSE removed_at END WHERE id=?""",
            (state, now, state, now, account_id))
        if account["source_home"] is not None:
            from connections.account_evidence import change_login, discovery_record, invalidate_discovery

            if state == "signed_in":
                from connections.harness_usage import clear_account_usage

                change_login(account, revision=expected_revision + 1, db=connection)
                clear_account_usage(account["provider"], account_id, db=connection)
            else:
                record = discovery_record(account)
                invalidate_discovery(account, login_seq=record["login_seq"],
                    auth_state=record["auth_state"], db=connection)
        return get_account(account_id, db=connection)


def record_evidence(account_id: str, *, identity: dict, expected_revision: int | None = None,
                    replace_comparison: bool = False, db=None) -> int:
    """Store this account's own vendor-reported identity facts and return its lifecycle revision.

    Only the provider profile's whitelisted string facts are kept and unknown
    stays absent, and a user-supplied ``hint`` is never overwritten. Evidence
    never bumps the revision. With ``expected_revision`` a changed or removed account
    is a conflict and nothing is written; without it a removed account's
    tombstone is left as it is.
    """
    from connections.account_profiles import profile_for

    with write_transaction(db, scope="vendor_account_evidence") as connection:
        if expected_revision is not None:
            account = _current(account_id, expected_revision, connection)
        else:
            account = get_account(account_id, db=connection)
            if account["state"] == "removed":
                return int(account["revision"])
        facts = profile_for(account["provider"]).stored_facts
        stored = dict(account["identity"])
        if replace_comparison:
            for key in facts:
                stored.pop(key, None)
        stored.update({key: value for key, value in identity.items()
                       if key in facts and key != "auth_cleanup"
                       and isinstance(value, str) and value})
        connection.execute("UPDATE vendor_accounts SET identity_json=?,updated_at=? WHERE id=?",
                           (json.dumps(stored, sort_keys=True), utc_now_iso(), account_id))
        return int(account["revision"])


def record_auth_cleanup(account_id: str, cleanup: dict | None, *, db=None) -> None:
    """Store a safe cleanup receipt separately from identity, including on tombstones."""
    if cleanup is not None and (
        not isinstance(cleanup, dict) or set(cleanup) != {"status", "cause", "at"}
        or cleanup.get("status") not in ("done", "failed", "skipped", "pending")
        or not isinstance(cleanup.get("cause"), (str, type(None))) or cleanup["cause"] not in AUTH_CLEANUP_CAUSES
        or not isinstance(cleanup.get("at"), str) or not cleanup["at"]
    ):
        raise AccountError("account_auth_cleanup_invalid")
    with write_transaction(db, scope="vendor_account_auth_cleanup") as connection:
        get_account(account_id, db=connection)
        connection.execute("UPDATE vendor_accounts SET auth_cleanup_json=?,updated_at=? WHERE id=?",
                           (json.dumps(cleanup) if cleanup is not None else None, utc_now_iso(), account_id))


def account_identity_key(account_id: str, *, db=None) -> str:
    return str(get_account(account_id, db=db)["id"])


def account_homes(account_id: str, *, db=None) -> tuple[Path | None, Path | None]:
    account = get_account(account_id, db=db)
    return (Path(account["source_home"]) if account["source_home"] else None,
            Path(account["runtime_home"]) if account["runtime_home"] else None)
