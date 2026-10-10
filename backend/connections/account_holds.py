"""Project account ineligibility into durable holds without moving bindings."""
from __future__ import annotations

from connections.account_policy import (
    PROJECT_POLICY_MISSING, AccountAdmissionError, account_reason, missing_project_reason, thread_scope,
)
from connections.vendor_accounts import get_account
from infra.time import utc_now_iso

ACCOUNT_HOLD_REASONS = (
    "account_project_denied", "account_removed", "account_signed_out", "account_runtime_pending",
    "account_model_not_discovered", "account_login_unverified", "account_home_mismatch",
    "codex_auth_projection_changed", "codex_auth_source_missing", PROJECT_POLICY_MISSING,
)



def refresh_thread_holds(thread_id: str, *, wake: bool = True, db) -> None:
    """Update per-vendor holds in the lifecycle/policy/rehome writer transaction."""
    scope = thread_scope(thread_id, db=db)
    missing = missing_project_reason(scope, db=db)
    saved = db.execute('SELECT model FROM threads WHERE id=?', (thread_id,)).fetchone()
    model = str(saved['model'] or '')
    from infra.vendor_turn_binding import model_vendor

    provider = model_vendor(model, db=db)
    rows = db.execute("SELECT provider,account_id FROM thread_vendor_accounts WHERE thread_id=?", (thread_id,)).fetchall()
    for binding in rows:
        account = get_account(str(binding["account_id"]), db=db)
        reason = missing or account_reason(
            account, provider=binding["provider"], project_ids=[scope["project_id"]], execution=True,
            model=model if binding['provider'] == provider else None, db=db)
        if reason:
            db.execute("""INSERT INTO thread_vendor_account_holds(thread_id,provider,account_id,reason,updated_at)
                VALUES (?,?,?,?,?) ON CONFLICT(thread_id,provider) DO UPDATE SET
                account_id=excluded.account_id,reason=excluded.reason,updated_at=excluded.updated_at""",
                (thread_id, binding["provider"], binding["account_id"], reason, utc_now_iso()))
        else:
            db.execute("DELETE FROM thread_vendor_account_holds WHERE thread_id=? AND provider=?",
                       (thread_id, binding["provider"]))
    from infra.queued_input_settlement import refresh_account_input_holds

    refresh_account_input_holds(db, thread_id)
    if wake and provider is not None and model_account_hold(thread_id, model, db=db) is None:
        from infra.unavailable_model_recovery import held_head_kind_sql
        from infra.write_effects import after_commit
        from infra.state.ws import schedule_models_changed

        held = db.execute(f"SELECT {held_head_kind_sql(':thread')} AS kind", {'thread': thread_id}).fetchone()
        if held['kind'] == 'account':
            after_commit(db, schedule_models_changed)


def affected_thread_ids(account_id: str, *, db) -> list[str]:
    """Current subjects resolved through the one account-selection owner."""
    from connections.account_policy import turn_account

    account = get_account(account_id, db=db)
    provider = account['provider']
    rows = db.execute("""SELECT t.id,t.model FROM threads t
        WHERE t.model IS NOT NULL AND trim(t.model)<>''
          AND EXISTS (SELECT 1 FROM models m WHERE m.harness=?
                      AND (m.id=trim(t.model) OR m.litellm_model=trim(t.model)))
          AND (EXISTS (SELECT 1 FROM thread_vendor_accounts b
                      WHERE b.thread_id=t.id AND b.provider=?)
               OR EXISTS (SELECT 1 FROM messages m WHERE m.thread_id=t.id
                          AND m.input_order IS NOT NULL AND m.delivery_state='queued'
                          AND m.active_turn_id IS NULL))""", (provider, provider)).fetchall()
    affected = []
    for row in rows:
        selected, _ = turn_account(provider, thread_id=str(row['id']), model=str(row['model']), db=db)
        if selected == account_id:
            affected.append(str(row['id']))
    return affected


def workspace_policy_thread_ids(provider: str, *, db) -> list[str]:
    """Threads whose effective provider policy follows the workspace."""
    return [str(row["id"]) for row in db.execute("""
        SELECT t.id FROM threads t
        LEFT JOIN project_policy_overrides o ON o.project_id=t.project_id
        WHERE o.project_id IS NULL AND (
          EXISTS (SELECT 1 FROM thread_vendor_accounts b WHERE b.thread_id=t.id AND b.provider=:provider)
          OR EXISTS (SELECT 1 FROM messages m WHERE m.thread_id=t.id AND m.input_order IS NOT NULL
                     AND m.delivery_state='queued' AND m.active_turn_id IS NULL))
        """, {"provider": provider}).fetchall()]


def policy_thread_ids(project_id: str, provider: str, *, db) -> list[str]:
    """Project threads a provider policy change can re-hold, found in one query.

    Holds exist only for bound accounts and queued input, so a thread with
    neither has nothing for the change to update.
    """
    return [str(row["id"]) for row in db.execute("""
        SELECT t.id FROM threads t WHERE t.project_id=:project AND (
          EXISTS (SELECT 1 FROM thread_vendor_accounts b WHERE b.thread_id=t.id AND b.provider=:provider)
          OR EXISTS (SELECT 1 FROM messages m WHERE m.thread_id=t.id AND m.input_order IS NOT NULL
                     AND m.delivery_state='queued' AND m.active_turn_id IS NULL))
        """, {"project": project_id, "provider": provider}).fetchall()]


def refresh_account_holds(account_id: str, *, wake: bool = True, db) -> None:
    """Project every affected thread's holds, reading each global fact once.

    Discovery changes many threads' holds at once and every thread re-reads the same
    project policy, account and model facts through the canonical readers. One scoped
    read memo (``connections.fact_memo``) spans the affected-set walk and the fan-out,
    so those readers answer from their first read; each thread's own scope, binding,
    pending account and queued head are still read per thread, and every evaluator
    stays the one this module already used.
    """
    from connections import fact_memo

    with fact_memo.fact_reads():
        for thread_id in affected_thread_ids(account_id, db=db):
            refresh_thread_holds(thread_id, wake=wake, db=db)


def refresh_model_hold(thread_id: str, model: str, *, wake: bool = True, db) -> str | None:
    """Refresh the durable projection and recheck the selected model together."""
    refresh_thread_holds(thread_id, wake=wake, db=db)
    return model_account_hold(thread_id, model, db=db)


def model_account_hold(thread_id: str, model: str, *, db) -> str | None:
    """Name an execution hold for the saved model without substituting it."""
    from connections.account_policy import require_claim
    from infra.vendor_turn_binding import model_vendor

    provider = model_vendor(model, db=db)
    if provider is None:
        return None
    try:
        require_claim(provider, thread_id=thread_id, model=model, db=db)
    except AccountAdmissionError as exc:
        return exc.reason
    return None
