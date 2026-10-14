"""Canonical scope-ladder provider policy for account providers."""
from __future__ import annotations

import json

from connections import fact_memo
from connections.vendor_accounts import AccountError, VENDORS, list_account_selection_facts, list_accounts
from infra.database import write_transaction


def capabilities(provider_key: str) -> dict:
    """Capabilities supported by this slice; later slices add models and self units."""
    if provider_key not in VENDORS:
        raise AccountError("provider policy is not available for this provider")
    return {"units": "accounts", "rollover": True}


def _row(project_id: str | None, provider_key: str, *, db):
    if project_id is None:
        return db.execute("SELECT rollover,order_json,disabled_json,revision FROM provider_policies "
                          "WHERE project_id IS NULL AND provider_key=?", (provider_key,)).fetchone()
    return db.execute("SELECT rollover,order_json,disabled_json,revision FROM provider_policies "
                      "WHERE project_id=? AND provider_key=?", (project_id, provider_key)).fetchone()


def _entry(provider_key: str, row, *, scope: str) -> dict:
    return {
        "provider_key": provider_key, "capabilities": capabilities(provider_key), "scope": scope,
        "rollover": bool(row["rollover"]) if row else False,
        "order": json.loads(row["order_json"]) if row and row["order_json"] else None,
        "disabled": json.loads(row["disabled_json"]) if row else [],
        "revision": row["revision"] if row else 0,
    }


def uses_workspace(project_id: str | None, *, db) -> bool:
    if project_id is None:
        return True
    return db.execute("SELECT 1 FROM project_policy_overrides WHERE project_id=?", (project_id,)).fetchone() is None


@fact_memo.memoized_reader
def effective_policy(project_id: str | None, provider_key: str, *, db) -> dict:
    """The only scope-policy reader: project policy/defaults, else workspace policy/defaults."""
    capabilities(provider_key)
    if project_id and not uses_workspace(project_id, db=db):
        return _entry(provider_key, _row(project_id, provider_key, db=db), scope="project")
    return _entry(provider_key, _row(None, provider_key, db=db), scope="workspace")


def _provider_policy_entry(project_id: str | None, provider_key: str, *, db) -> dict:
    """Build the public representation for one effective provider policy."""
    policy = effective_policy(project_id, provider_key, db=db)
    ordered = _in_saved_order(list_accounts(provider=provider_key, db=db), policy["order"])
    return {
        **policy,
        "units": [{"id": account["id"], "label": account["name"], "enabled": account["id"] not in policy["disabled"]}
                  for account in ordered],
    }


def _validate(provider_key: str, *, rollover, order, disabled, db) -> None:
    capability = capabilities(provider_key)
    if type(rollover) is not bool:
        raise AccountError("rollover must be a boolean")
    if order is not None and (not isinstance(order, list) or any(not isinstance(item, str) for item in order)
                              or len(order) != len(set(order))):
        raise AccountError("order must be an ordered list of account ids or null")
    if not isinstance(disabled, list) or any(not isinstance(item, str) for item in disabled) or len(disabled) != len(set(disabled)):
        raise AccountError("disabled must be a list of account ids")
    ids = {account["id"] for account in list_accounts(provider=provider_key, include_removed=True, db=db)
           if account["state"] != "removed"}
    if any(item not in ids for item in (order or []) + disabled):
        raise AccountError("policy account must belong to this provider and not be removed")
    if rollover and not capability["rollover"]:
        raise AccountError("rollover is not available for this provider")


def _upsert(project_id: str | None, provider_key: str, *, rollover: bool, order, disabled: list[str], revision: int, db) -> None:
    db.execute("""INSERT INTO provider_policies(project_id,provider_key,rollover,order_json,disabled_json,revision)
                  VALUES (?,?,?,?,?,?)
                  ON CONFLICT DO UPDATE SET rollover=excluded.rollover,order_json=excluded.order_json,
                    disabled_json=excluded.disabled_json,revision=excluded.revision""",
               (project_id, provider_key, int(rollover), json.dumps(order) if order is not None else None,
                json.dumps(disabled), revision))


def _clone_workspace_policies(project_id: str, *, db) -> None:
    for row in db.execute("SELECT provider_key,rollover,order_json,disabled_json,revision FROM provider_policies WHERE project_id IS NULL").fetchall():
        _upsert(project_id, row["provider_key"], rollover=bool(row["rollover"]),
                order=json.loads(row["order_json"]) if row["order_json"] else None,
                disabled=json.loads(row["disabled_json"]), revision=row["revision"], db=db)


def set_policy(project_id: str | None, provider_key: str, *, rollover: bool, order,
               disabled: list[str], expected_revision: int, db=None) -> dict:
    """The only writer for one effective provider policy entry."""
    with write_transaction(db, scope="provider_policy") as connection:
        if project_id is not None:
            if not connection.execute("SELECT 1 FROM projects WHERE id=?", (project_id,)).fetchone():
                raise AccountError("project does not exist")
            if uses_workspace(project_id, db=connection):
                connection.execute("INSERT INTO project_policy_overrides(project_id,revision) VALUES (?,0)", (project_id,))
                _clone_workspace_policies(project_id, db=connection)
        # Canonical callers provide all policy facts explicitly.
        before = effective_policy(project_id, provider_key, db=connection)
        if before["revision"] != expected_revision:
            raise AccountError("provider policy revision changed; refresh before acting")
        _validate(provider_key, rollover=rollover, order=order, disabled=disabled, db=connection)
        _upsert(project_id, provider_key, rollover=rollover, order=order, disabled=disabled,
                revision=expected_revision + 1, db=connection)
        _refresh(project_id, provider_key, db=connection)
        return _provider_policy_entry(project_id, provider_key, db=connection)


def set_use_workspace(project_id: str, *, use_workspace: bool, expected_revision: int, db=None) -> dict:
    """Toggle a project override; turning it off clones workspace rows, on discards them."""
    with write_transaction(db, scope="project_provider_policy") as connection:
        if not connection.execute("SELECT 1 FROM projects WHERE id=?", (project_id,)).fetchone():
            raise AccountError("project does not exist")
        existing = connection.execute("SELECT revision FROM project_policy_overrides WHERE project_id=?", (project_id,)).fetchone()
        revision = existing["revision"] if existing else 0
        if revision != expected_revision:
            raise AccountError("project provider-policy revision changed; refresh before acting")
        if use_workspace:
            connection.execute("DELETE FROM provider_policies WHERE project_id=?", (project_id,))
            connection.execute("DELETE FROM project_policy_overrides WHERE project_id=?", (project_id,))
        elif existing is None:
            connection.execute("INSERT INTO project_policy_overrides(project_id,revision) VALUES (?,?)", (project_id, 1))
            _clone_workspace_policies(project_id, db=connection)
        _refresh(project_id, None, db=connection)
        return policy_payload(project_id, db=connection)


def ordered_accounts(provider_key: str, policy: dict, *, selection_only: bool = False, db) -> list[dict]:
    accounts = (list_account_selection_facts(provider_key, db=db) if selection_only
                else list_accounts(provider=provider_key, db=db))
    return _in_saved_order([account for account in accounts if account["id"] not in policy["disabled"]], policy["order"])


def _in_saved_order(accounts: list[dict], order) -> list[dict]:
    """The one rollover-order rule: saved order first, then every other account in provider order."""
    if order is None:
        return accounts
    by_id = {account["id"]: account for account in accounts}
    return [by_id[item] for item in order if item in by_id] + [account for account in accounts if account["id"] not in order]


def policy_payload(project_id: str | None, *, db) -> dict:
    providers = []
    for provider_key in VENDORS:
        providers.append(_provider_policy_entry(project_id, provider_key, db=db))
    payload: dict[str, object] = {"scope": "workspace" if project_id is None else "project", "providers": providers}
    if project_id is not None:
        override = db.execute("SELECT revision FROM project_policy_overrides WHERE project_id=?", (project_id,)).fetchone()
        payload |= {"use_workspace": override is None, "override_revision": override["revision"] if override else 0}
    else:
        payload["overriding_projects"] = [dict(row) for row in db.execute(
            "SELECT p.id,p.name FROM projects p JOIN project_policy_overrides o ON o.project_id=p.id ORDER BY p.name,p.id").fetchall()]
    return payload


def _refresh(project_id: str | None, provider_key: str | None, *, db) -> None:
    from connections.account_holds import policy_thread_ids, refresh_thread_holds, workspace_policy_thread_ids
    from infra.connection_limit_release import limited_project_threads, limited_workspace_threads
    from infra.connection_limit_drive import schedule_limited_heads
    from infra.write_effects import after_commit_merged

    providers = [provider_key] if provider_key else list(VENDORS)
    for provider in providers:
        if project_id is None:
            thread_ids = workspace_policy_thread_ids(provider, db=db)
            limited = limited_workspace_threads(provider, db=db)
        else:
            thread_ids = policy_thread_ids(project_id, provider, db=db)
            limited = limited_project_threads(project_id, provider, db=db)
        for thread_id in thread_ids:
            refresh_thread_holds(thread_id, db=db)
        if limited:
            after_commit_merged(db, ("limited_heads", provider), limited, schedule_limited_heads)
