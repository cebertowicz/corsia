"""Scoped subscription projection with a display-only capability fallback."""
from __future__ import annotations

from connections import fact_memo
from connections.account_policy import account_reason, pending_account, permitted_accounts, thread_scope, turn_account
from connections.harness_usage import project_for_model, stored_usage
from connections.vendor_accounts import get_account
from infra.database import get_db

_DEFAULT_EVIDENCE = ("resolved_identity", "is_default", "alias_of", "cache_marker")


def account_model_status(account: dict, row: dict, *, project_ids: list, db, move: bool = False) -> dict:
    """The one decision whether ``row``'s model can run on ``account`` for these projects.

    ``row`` is the account-independent catalog row. Each account uses its own
    discovery, lifecycle, project policy and usage evidence. Returns ``status``,
    ``status_reason`` and ``usage``.
    """
    from connections.account_evidence import discovery_view
    from llm.model_status import USAGE_LIMITED

    model = row["litellm_model"]
    provider = account["provider"]
    usage = project_for_model(stored_usage(provider=provider, account_id=account["id"], db=db), model=model, provider=provider)
    reason: str | None
    if row.get("status_reason") == "connection_unavailable":
        status, reason = row["status"], row.get("status_reason")
    else:
        own = discovery_view(account)["discovery"].get("models", {}).get(model, {})
        status = "ready" if own.get("available", True) else "unavailable"
        reason = None if status == "ready" else own.get("availability_reason") or "discovered_model_unavailable"
    policy = account_reason(account, provider=provider, project_ids=project_ids, execution=True, model=model, db=db)
    if policy:
        status, reason = "unavailable", policy
    elif status == "ready" and not usage["usable"]:
        status, reason = "unavailable", USAGE_LIMITED
    return {"status": status, "status_reason": reason if status != "ready" else None, "usage": usage}


@fact_memo.fact_reads()
def project_accounts(rows, *, thread_id=None, project_id=None, account_id=None, db=None):
    """Add ``runs_on`` to each harness row: whether its model runs on each permitted account.

    The row's own ``status`` stays what the model is, never what one account can
    do now. The entry marked ``selected`` is the one answer to which account this
    scope's next turn of that model runs on (``turn_account``): explicit,
    thread, project, workspace; it carries ``pending`` when a person chose it
    during a running turn and the next claim has not moved there yet. Only that
    account's discovered evidence replaces the row's context window and
    identity; its account-independent capabilities
    stay for display when the selected account does not offer the model. A scope
    whose policy permits no account for the turn reads ``account_project_denied``.
    """
    connection = db or get_db()
    try:
        scope = thread_scope(thread_id, db=connection) if thread_id else None
        project = scope["project_id"] if scope else project_id
        requested = get_account(account_id, db=connection) if account_id is not None else None
        from llm.locality import locality_cause, project_cloud_models_off

        cloud_refusal = locality_cause(
            thread_local_only=bool(scope and scope["local_only"]),
            cloud_off=project_cloud_models_off(project, db=connection),
        )
        result = []
        pending: dict[str, str | None] = {}
        for original in rows:
            row = dict(original)
            provider = row.get("connection")
            if provider not in ("codex", "claude-code"):
                _apply_locality(row, cloud_refusal)
                result.append(row)
                continue
            permitted = permitted_accounts(provider, project_ids=[project], db=connection)
            explicit = account_id if requested is not None and requested["provider"] == provider else None
            selected, selection_reason = turn_account(provider, thread_id=thread_id, project_id=project,
                                                      account_id=explicit, model=row["litellm_model"], db=connection)
            row["runs_on"] = [_runs_on(item, row, project, selected, connection) for item in permitted]
            if thread_id and explicit is None:
                if provider not in pending:
                    pending[provider] = pending_account(thread_id, provider, db=connection)
                for entry in row["runs_on"]:
                    if entry["selected"] and entry["account_id"] == pending[provider]:
                        entry["pending"] = True
            chosen = next((item for item in permitted if item["id"] == selected), None)
            if chosen is None:
                if selection_reason == "account_project_denied" or selected is not None:
                    _clear_evidence(row)
                    row.update(status="unavailable", status_reason="account_project_denied")
            elif chosen["source_home"] is not None:
                _read_discovered_evidence(row, chosen, connection)
            row["runnable"] = _runnable_status(row)
            _apply_locality(row, cloud_refusal)
            result.append(row)
        return result
    finally:
        if db is None:
            connection.close()



def _apply_locality(row: dict, refusal: str | None) -> None:
    """Overlay project or thread locality policy without deleting scoped catalog rows."""
    if refusal and not row.get("is_local", False):
        row.update(status="unavailable", status_reason=refusal)
        if "runnable" in row:
            row["runnable"] = {"status": "unavailable", "status_reason": refusal}


def _runs_on(account, row, project, selected, db) -> dict:
    """One account's whole decision for the model, as ``account_model_status`` made it.

    ``usage`` is that decision's own projection (the windows that bind this
    model) and ``usage_check`` the account's limit recheck as the picker shows
    it, so no reader derives either again.
    """
    from connections.harness_usage_check import is_checking
    from llm.model_status import USAGE_LIMITED

    decision = account_model_status(account, row, project_ids=[project], db=db)
    usage = decision["usage"]
    limited = decision["status_reason"] == USAGE_LIMITED
    entry = {"account_id": account["id"], "selected": account["id"] == selected, "status": decision["status"],
             "status_reason": decision["status_reason"], "resets_at": usage.get("resets_at") if limited else None,
             "usage": usage}
    if limited and is_checking(account["id"]):
        entry["usage_check"] = "checking"
    elif limited and isinstance(usage.get("check"), dict) and usage["check"].get("error"):
        entry.update(usage_check="failed", usage_check_error=usage["check"]["error"])
    return entry


def _runnable_status(row: dict) -> dict:
    """Return whether this scoped harness row's model is available now.

    A ready sibling makes the model available when the selected account is
    usage-limited. The selected ``runs_on`` entry remains the account the next
    turn uses, including when rollover is off and that turn stays held there.
    """
    from llm.model_status import USAGE_LIMITED

    selected = next((entry for entry in row["runs_on"] if entry["selected"]), None)
    if selected is None:
        return {"status": row["status"], "status_reason": row.get("status_reason")}
    if selected["status"] == "ready" or (
        selected["status_reason"] == USAGE_LIMITED
        and any(entry["status"] == "ready" for entry in row["runs_on"])
    ):
        return {"status": "ready", "status_reason": None}
    return {"status": "unavailable", "status_reason": selected["status_reason"]}


def _read_discovered_evidence(row, account, db) -> None:
    """Use selected discovery for readiness, retaining base capabilities only for display."""
    from connections.account_evidence import discovery_record, readiness_reason

    display_capabilities = row["capabilities"]
    _clear_evidence(row)
    row.update(context_window=None, capabilities=display_capabilities, resolved_identity=None)
    if discovery_record(account)["discovery"]["status"] == "unknown":
        return
    if readiness_reason(account, model=row["litellm_model"]) is None:
        own = discovery_record(account)["discovery"]["models"][row["litellm_model"]]
        row.update({key: own.get(key) for key in ("context_window", "resolved_identity")})
        row["capabilities"] = _canonical_capabilities(row, own, db=db)


def effective_status(row: dict) -> tuple[str | None, str | None]:
    """A scoped row's ``(status, status_reason)`` on the account its next turn runs on.

    A row with no selected ``runs_on`` entry (not a harness model, or a scope
    that denies it) is its own status.
    """
    entry = next((item for item in row.get("runs_on") or () if item.get("selected")), None)
    if entry is None:
        return row.get("status"), row.get("status_reason")
    return entry["status"], entry["status_reason"]


def _canonical_capabilities(row: dict, own: dict, *, db) -> dict:
    """One added account's capability block through the catalog's single owner.

    Every other row's block is assembled by ``llm.model_status._capabilities_for_row``
    (the one place that resolves ``reasoning_config`` from the connector snapshot
    and the row's discovered levels). An added account's rows used to carry its
    raw discovery record verbatim, so the switcher lost that resolved block —
    ``reasoning_config`` particularly — and the reasoning control disappeared for
    every model on an added account.
    """
    from llm.context_window import resolve_window_for_row
    from llm.model_status import _capabilities_for_row

    # The window comes from the one resolver, never from this row's stored copy
    # (I7). The public entry carries no harness field, and only harness rows
    # reach here, so name it from ``connection`` for the harness cap rule.
    return _capabilities_for_row(
        row["connection"], row["connection"], row,
        {"discovered_capabilities": own.get("capabilities") or {}},
        resolve_window_for_row({**row, "harness": row["connection"]}, db=db),
    )


def _clear_evidence(row):
    row.update(context_window=None, capabilities={}, resolved_identity=None)
    for key in _DEFAULT_EVIDENCE:
        row.pop(key, None)
