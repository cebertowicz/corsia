"""Account binding at canonical thread/model creation boundaries."""
from __future__ import annotations

from connections.account_policy import AccountAdmissionError, select_account
from infra.database import write_transaction


def bind_model_account(thread_id: str, model: str | None, *, account_id: str | None = None,
                       actor_thread_id: str | None = None, defer_receipt: bool = False, db) -> str | None:
    """Bind the selected model's vendor while keeping its exact selector intact."""
    from infra.vendor_turn_binding import model_vendor

    provider = model_vendor(str(model or ""), db=db)
    if provider is None:
        if account_id is not None:
            raise AccountAdmissionError("account_requires_vendor_model")
        return None
    existing = db.execute("SELECT account_id,pending_account_id FROM thread_vendor_accounts "
                          "WHERE thread_id=? AND provider=?", (thread_id, provider)).fetchone()
    # A person's held choice never rolls over; the claim moves to it.
    if account_id is None and existing is not None and existing['pending_account_id'] is None:
        from connections.account_policy import thread_scope
        from connections.account_rollover import rollover_destination
        from connections.provider_policy import effective_policy
        from llm.model_status import harness_base_row

        project = thread_scope(thread_id, db=db)['project_id']
        if effective_policy(project, provider, db=db)['rollover']:
            destination = rollover_destination(provider, existing['account_id'], harness_base_row(str(model), db=db),
                                                project, db=db)
            from connections.account_moves import movable_thread

            context = db.execute("SELECT user_id,runtime_context_epoch FROM threads WHERE id=?", (thread_id,)).fetchone()
            if destination is not None and context is not None and movable_thread(thread_id, db=db):
                rebind_idle_account(thread_id, destination, expected_runtime_epoch=context['runtime_context_epoch'],
                                    user_id=context['user_id'], provider=provider,
                                    reason=_move_reason(existing['account_id'], provider, project, db=db),
                                    defer_receipt=defer_receipt, db=db)
                return destination
    return select_account(thread_id, provider, account_id=account_id, actor_thread_id=actor_thread_id,
                          model=model, db=db)


def _move_reason(source_id: str, provider: str, project: str | None, *, db) -> str:
    """Why a pick-time rollover leaves the account the thread is bound to.

    A bound account that was signed out, removed or turned off for the project is
    named as that, never as a limit; otherwise it is limited.
    """
    from connections.account_policy import account_reason
    from connections.vendor_accounts import get_account

    reason = account_reason(get_account(source_id, db=db), provider=provider, project_ids=[project],
                            execution=False, db=db)
    return {'account_project_denied': 'account_not_permitted', 'account_removed': 'account_removed',
            'account_signed_out': 'account_signed_out'}.get(reason, 'account_limit')


def retire_provider_sessions(thread_id: str, provider: str, *, db) -> None:
    """Retire the durable provider session a thread's old account created.

    The one owner for every committed account-binding change: a provider session
    lives under its account's home, so the next turn must start fresh instead of
    resuming an id the new account cannot see. Runs inside the binding
    transaction, so no turn can claim between the move and the retirement.
    """
    if provider == "codex":
        from subagents.codex_runtime import _retire_registered_thread_artifacts

        _retire_registered_thread_artifacts(thread_id, db=db)
    elif provider == "claude-code":
        from subagents.cc_shutdown import retire_provider_session

        retire_provider_session(thread_id, db=db)


def rebind_idle_account(thread_id: str, account_id: str, *, user_id: str, expected_runtime_epoch: int | None = None,
                        provider: str | None = None, reason: str = 'account_pinned',
                        defer_receipt: bool = False, db=None) -> dict:
    """Change only an idle account binding and invalidate vendor continuation.

    ``provider`` names the binding being rebound when the caller already resolved it
    from the model it is binding — a switch binds its destination, not the model
    being left. Without it the thread's own model names the provider, unless a person
    chose another vendor's account: that vendor is not running, so the choice is created
    through ``select_account`` or held for its next claim, and the running vendor is untouched.

    ``expected_runtime_epoch`` is for a caller that read the thread's context earlier in the
    same flow and must not move a thread that changed since; a person's explicit choice passes
    none, because the idle, vendor and allowlist checks below already guard the write.

    ``changed`` says whether the binding was written; ``pending`` that the choice applies at a
    later claim (held behind a running turn, or until the thread runs that vendor).
    """
    from connections.account_moves import commit_move, hold_pending_account, movable_thread
    from connections.account_policy import admit_account, thread_project_scopes
    from connections.vendor_accounts import get_account
    from infra.vendor_turn_binding import model_vendor

    with write_transaction(db, scope="rebind_idle_vendor_account") as connection:
        row = connection.execute("SELECT model,user_id,runtime_context_epoch,switch_operation_id,lifecycle_state "
                                 "FROM threads WHERE id=?", (thread_id,)).fetchone()
        if row is None or row["user_id"] != user_id:
            raise AccountAdmissionError("account_thread_missing")
        if expected_runtime_epoch is not None and row["runtime_context_epoch"] != expected_runtime_epoch:
            raise AccountAdmissionError("account_runtime_revision_changed")
        # A person's choice on an active thread busy with a turn applies at the next claim, as a model choice does.
        holds = reason == 'account_pinned' and expected_runtime_epoch is None and row["lifecycle_state"] == 'active'
        if not holds and not movable_thread(thread_id, db=connection):
            raise AccountAdmissionError("account_change_requires_idle")
        account = get_account(account_id, db=connection)
        running = model_vendor(str(row["model"] or ""), db=connection)
        dormant = provider is None and holds and str(account["provider"]) != running
        provider = provider or (str(account["provider"]) if dormant else running)
        if provider is None:
            raise AccountAdmissionError("account_requires_vendor_model")
        admit_account(provider, account["id"], thread_project_scopes(thread_id, None, db=connection), execution=False, db=connection)
        bound = connection.execute("SELECT 1 FROM thread_vendor_accounts WHERE thread_id=? AND provider=?",
                                   (thread_id, provider)).fetchone()
        if bound is None:
            # No binding for this vendor yet, so there is nothing to move: create it.
            select_account(thread_id, provider, account_id=account_id, db=connection)
            return {'account_id': account_id, 'runtime_context_epoch': row['runtime_context_epoch'],
                    'changed': True, 'pending': dormant}
        if dormant or not movable_thread(thread_id, db=connection):
            hold_pending_account(thread_id, provider, account_id, db=connection)
            return {'account_id': account_id, 'runtime_context_epoch': row['runtime_context_epoch'],
                    'changed': False, 'pending': True}
        result = commit_move(thread_id, provider, account_id, reason, db=connection,
                             defer_receipt=defer_receipt)
        return {key: result[key] for key in ('account_id', 'runtime_context_epoch', 'changed')}
