"""One account selection/admission owner for creation, FIFO and provider writes."""
from __future__ import annotations

from connections import fact_memo
from connections.provider_policy import effective_policy, ordered_accounts, set_policy
from connections.vendor_accounts import VENDORS, AccountError, account_selection_facts, get_account, resolve_account_id
from infra.database import get_db, write_transaction

RUNTIME_PENDING = "account_runtime_pending"
PROJECT_POLICY_MISSING = "project_policy_missing"
NOT_INFERENCE = "account_provider_not_inference"
NOT_CONFIGURED = "account_not_configured"


def read_account_readiness(thread_ids, *, db):
    """Advisory batch using the same fact evaluator as require_claim (stored records plus its home check)."""
    from connections.account_readiness import read_account_readiness as read

    return read(thread_ids, db=db)


class AccountAdmissionError(AccountError):
    """A named account hold with alternatives restricted to both actor scopes."""

    def __init__(self, reason: str, alternatives: list[dict] | None = None):
        self.reason = reason
        self.alternatives = alternatives or []
        super().__init__(reason + "; choose a permitted account or restore this account's eligibility")


def thread_scope(thread_id: str, *, db) -> dict:
    """Derive ownership and project from durable server-side thread identity."""
    row = db.execute(
        "SELECT user_id,project_id,local_only FROM threads WHERE id=?", (thread_id,),
    ).fetchone()
    if row is None:
        raise AccountAdmissionError("account_thread_missing")
    return dict(row)


def missing_project_reason(scope: dict, *, db) -> str | None:
    """Hold a thread whose named project row is gone: no policy is readable, so never fail open.

    A thread with no project at all stays unrestricted.
    """
    project = scope["project_id"]
    if project and not project_exists(project, db=db):
        return PROJECT_POLICY_MISSING
    return None


@fact_memo.memoized_reader
def project_exists(project_id: str, *, db) -> bool:
    """Whether the named project row exists: the one read-answer for a project fact."""
    return db.execute("SELECT 1 FROM projects WHERE id=?", (project_id,)).fetchone() is not None


def _governed(scope: dict, *, db) -> str | None:
    reason = missing_project_reason(scope, db=db)
    if reason:
        raise AccountAdmissionError(reason)
    return scope["project_id"]


def thread_project_scopes(thread_id, actor_thread_id, *, db):
    """The governed project ids an account must be admitted under for this thread and actor."""
    destination = thread_scope(thread_id, db=db) if thread_id else {"project_id": None}
    projects = [_governed(destination, db=db)]
    if actor_thread_id:
        actor = thread_scope(actor_thread_id, db=db)
        if thread_id and actor["user_id"] != destination["user_id"]:
            raise AccountAdmissionError("account_actor_unauthorized")
        projects.append(_governed(actor, db=db))
    return projects


def public_account(account: dict) -> dict:
    """Project only account metadata that a permitted selector needs."""
    return {field: account[field] for field in ("id", "provider", "name", "is_default", "state", "revision")}


def _policies(provider: str, project_ids: list, *, db) -> list[dict]:
    """Return the effective policy at every active account scope."""
    return [effective_policy(project, provider, db=db) for project in (project_ids or [None])]


def permitted_accounts(provider: str, *, project_ids: list, selection_only: bool = False, db,
                       accounts: list[dict] | None = None) -> list[dict]:
    """Intersect effective scope policies while preserving destination order."""
    policies = _policies(provider, project_ids, db=db)
    ordered = ordered_accounts(provider, policies[0], selection_only=selection_only, db=db, accounts=accounts)
    return [account for account in ordered if all(account["id"] not in policy["disabled"] for policy in policies)]


def account_fact_reason(account: dict | None, *, provider: str, policies: list,
                        execution: bool, model: str | None = None) -> str | None:
    """Evaluate loaded account/policy facts for both claim and advisory batches."""
    if account is None:
        return NOT_CONFIGURED
    if account["provider"] != provider:
        return "account_vendor_mismatch"
    if any(account["id"] in p["disabled"] for p in policies):
        return "account_project_denied"
    if account["state"] == "removed":
        return "account_removed"
    if account["state"] == "signed_out":
        return "account_signed_out"
    if execution:
        from connections.account_evidence import readiness_reason

        return readiness_reason(account, model=model)
    return None


def account_reason(account: dict, *, provider: str, project_ids: list, execution: bool,
                   model: str | None = None, db) -> str | None:
    """Evaluate lifecycle and project state without interpreting usage limits."""
    reason = account_fact_reason(account, provider=provider, execution=execution, model=model,
                                 policies=_policies(provider, project_ids, db=db))
    return reason


def _candidate(thread_id, provider, account_id, projects, *, db):
    if account_id is not None:
        return resolve_account_id(provider, account_id, db=db)
    if thread_id:
        binding = db.execute("SELECT account_id FROM thread_vendor_accounts WHERE thread_id=? AND provider=?",
                             (thread_id, provider)).fetchone()
        if binding:
            return str(binding["account_id"])
    candidates = permitted_accounts(provider, project_ids=projects, selection_only=True, db=db)
    if candidates:
        return candidates[0]["id"]
    # With every account disabled, keep the provider Default for attribution; admission reports its denial.
    return resolve_account_id(provider, db=db)


def require_inference(provider: str) -> None:
    """Only a built-in vendor may be bound to a thread or launched."""
    if provider not in VENDORS:
        raise AccountAdmissionError(NOT_INFERENCE)


_READ = object()


def _turn_identity(provider: str, *, thread_id: str | None, project_id: str | None,
                   account_id: str | None, model: str | None, db, binding=_READ) -> str | None:
    """The account a new turn of ``model`` runs on, before any usability reason.

    The thread's binding, else the project default, else the workspace Default.
    Where the project opted into rollover and that account is limited for
    ``model``, the first usable account along the order instead. An explicit
    ``account_id`` is the caller's own choice and never rolls over. A caller that
    already read the thread's binding row (or its absence, ``None``) passes it, so
    the answer is the same without a second read.
    """
    if binding is _READ:
        binding = db.execute("SELECT account_id,pending_account_id FROM thread_vendor_accounts "
                             "WHERE thread_id=? AND provider=?",
                             (thread_id, provider)).fetchone() if thread_id and account_id is None else None
    if binding is not None and binding["pending_account_id"] is not None:
        # A person's choice made during a running turn: the next claim moves to it, and it never rolls over.
        return str(binding["pending_account_id"])
    # The binding was read above; with none, the thread falls back to the project's order.
    source = str(binding["account_id"]) if binding is not None else _candidate(
        None, provider, account_id, [project_id], db=db)
    if account_id is not None or model is None or source is None:
        return source
    if not effective_policy(project_id, provider, db=db)["rollover"]:
        return source
    from connections.account_rollover import rollover_destination
    from llm.model_status import harness_base_row

    return rollover_destination(provider, source, harness_base_row(model, db=db), project_id, db=db) or source


def pending_account(thread_id: str, provider: str, *, db) -> str | None:
    """The account a person chose while a turn was running, which the next claim moves to."""
    row = db.execute("SELECT pending_account_id FROM thread_vendor_accounts WHERE thread_id=? AND provider=?",
                     (thread_id, provider)).fetchone()
    return str(row[0]) if row is not None and row[0] is not None else None


def turn_account(provider: str, *, thread_id: str | None = None, project_id: str | None = None,
                 account_id: str | None = None, model: str | None = None, db) -> tuple[str | None, str | None]:
    """The one answer to which account a new turn runs on, and why it is unusable if it is.

    The scope is the thread's project when a thread is given, else ``project_id``.
    Without a project the workspace policy applies. Without ``model`` no
    limit-driven move is considered: the answer is the account the turn starts
    from. ``(id, None)`` means usable.
    """
    require_inference(provider)
    project = thread_scope(thread_id, db=db)["project_id"] if thread_id else project_id
    return _scoped_turn_account(provider, thread_id=thread_id, project=project, account_id=account_id,
                                model=model, db=db)


def _scoped_turn_account(provider: str, *, thread_id: str | None, project: str | None,
                         account_id: str | None, model: str | None, db,
                         binding=_READ) -> tuple[str | None, str | None]:
    """``turn_account`` for a caller that already holds the thread's project, and optionally its binding."""
    projects = [project] if project is not None else []
    for item in projects:
        if not project_exists(item, db=db):
            return None, PROJECT_POLICY_MISSING
    identity = _turn_identity(provider, thread_id=thread_id, project_id=project, account_id=account_id,
                              model=model, db=db, binding=binding)
    if identity is None:
        policies = _policies(provider, projects, db=db)
        return None, "account_project_denied" if any(policy["disabled"] for policy in policies) else NOT_CONFIGURED
    try:
        account = account_selection_facts(identity, db=db)
    except AccountError:
        # A deleted binding remains missing evidence for advisory readiness;
        # callers must not turn that read into a database exception.
        return identity, 'account_removed'
    return identity, account_reason(account, provider=provider, project_ids=projects, execution=False, db=db)


def scope_account(provider: str, *, project_id: str | None, db) -> tuple[str | None, str | None]:
    """The account a project scope would use and why it is unusable, without a thread or a write."""
    return turn_account(provider, project_id=project_id, db=db)


def admit_account(provider, identity, projects, *, execution, model=None, db):
    """Admit an account under workspace policy for these projects, or raise AccountAdmissionError.

    The one admission rule shared by every account writer: ``execution`` adds the launch checks.
    """
    if identity is None:
        # No account is selectable; an explicit disabled policy is a denial, not ambient absence.
        policies = _policies(provider, projects, db=db)
        if any(policy["disabled"] for policy in policies):
            raise AccountAdmissionError("account_project_denied")
        reason = account_fact_reason(None, provider=provider, execution=execution, model=model, policies=policies)
        if reason:
            raise AccountAdmissionError(reason)
        return None
    account = get_account(identity, db=db)
    reason = account_reason(account, provider=provider, project_ids=projects,
                            execution=execution, model=model, db=db)
    if reason:
        alternatives = [public_account(item) for item in permitted_accounts(provider, project_ids=projects, db=db)
                        if item["state"] in ("legacy", "signed_in")]
        raise AccountAdmissionError(reason, alternatives)
    return identity


def select_account(thread_id: str, provider: str, *, account_id: str | None = None,
                   actor_thread_id: str | None = None, wake: bool = True,
                   model: str | None = None, db) -> str | None:
    """Choose once by explicit/binding/project/workspace precedence and persist."""
    require_inference(provider)
    with write_transaction(db, scope="select_vendor_account") as connection:
        projects = thread_project_scopes(thread_id, actor_thread_id, db=connection)
        identity = _candidate(thread_id, provider, account_id, projects, db=connection)
        existing = connection.execute("SELECT account_id FROM thread_vendor_accounts WHERE thread_id=? AND provider=?",
                                      (thread_id, provider)).fetchone()
        if account_id is None and model is not None and existing is None and len(projects) == 1:
            identity = _turn_identity(provider, thread_id=thread_id, project_id=projects[0], account_id=None,
                                      model=model, db=connection)
        identity = admit_account(provider, identity, projects, execution=False, db=connection)
        if existing and str(existing["account_id"]) != identity:
            raise AccountAdmissionError("account_change_requires_idle")
        if identity is not None and existing is None:
            connection.execute("INSERT INTO thread_vendor_accounts(thread_id,provider,account_id) VALUES (?,?,?)",
                               (thread_id, provider, identity))
        from connections.account_holds import refresh_thread_holds

        refresh_thread_holds(thread_id, wake=wake, db=connection)
        return identity


def chosen_account_id(thread_id: str, provider: str, *, db) -> str | None:
    """Freeze metadata choice without rebinding or authorizing a provider write."""
    projects = thread_project_scopes(thread_id, None, db=db)
    return _candidate(thread_id, provider, None, projects, db=db)


def _write_choice(provider, thread_id, account_id, turn_id, actor_thread_id, frozen, *, db):
    require_inference(provider)
    projects = thread_project_scopes(thread_id, actor_thread_id, db=db)
    if turn_id is not None:
        row = db.execute("SELECT vendor_account_id FROM active_turns WHERE id=? AND thread_id=?",
                         (turn_id, thread_id)).fetchone()
        if row is None:
            raise AccountAdmissionError("account_turn_missing")
        identity = row["vendor_account_id"]
        if (frozen or account_id is not None) and identity != account_id:
            raise AccountAdmissionError("account_turn_mismatch")
    else:
        identity = account_id if frozen else _candidate(thread_id, provider, account_id, projects, db=db)
    return identity, projects


def require_claim(provider: str, *, thread_id: str | None, model: str,
                  turn_id: str | None = None, actor_thread_id: str | None = None,
                  account_id: str | None = None, frozen: bool = False, db=None) -> str | None:
    """Admit durable current choice or a frozen Turn without constructing any home."""
    connection = db or get_db()
    try:
        identity, projects = _write_choice(provider, thread_id, account_id, turn_id, actor_thread_id, frozen, db=connection)
        return admit_account(provider, identity, projects, execution=True, model=model, db=connection)
    finally:
        if db is None:
            connection.close()


def require_launch(provider: str, *, thread_id: str | None = None, account_id: str | None = None,
                   turn_id: str | None = None, actor_thread_id: str | None = None,
                   frozen: bool = False, model: str | None = None, launch=None, receipt=None, db=None) -> str | None:
    """Policy/lifecycle first, then concrete frozen home and lifecycle/login evidence."""
    connection = db or get_db()
    try:
        identity, projects = _write_choice(provider, thread_id, account_id, turn_id, actor_thread_id, frozen, db=connection)
        admit_account(provider, identity, projects, execution=False, db=connection)
        account = get_account(identity, db=connection) if identity is not None else None
        if account is not None and account['source_home'] is not None and launch is None:
            raise AccountAdmissionError(RUNTIME_PENDING)
        admit_account(provider, identity, projects, execution=True, model=model, db=connection)
        if launch is not None:
            from connections.account_runtime import validate_launch

            validate_launch(account, launch=launch, receipt=receipt, model=model, db=connection)
        return identity
    finally:
        if db is None:
            connection.close()
