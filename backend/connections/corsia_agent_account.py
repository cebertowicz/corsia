"""Bind the CORSIA agent, before its first model, to an account a person chose.

The first-run screen runs before the conductor has a model, so the thread-account
route cannot read a vendor from it. This step takes the vendor from the account.
Neither existing writer is enough on its own: ``select_account`` admits without the
execution check that covers limits, and ``commit_move`` does no admission. So this
step checks readiness itself and then, in one write transaction, rechecks that the
conductor is idle and still has no model, admits the account under workspace
policy, and writes a first binding or a move. It never changes the workspace
default. Every refusal writes nothing and is named in ``REFUSALS``.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
import logging
from typing import Any

from connections.account_policy import AccountAdmissionError, admit_account, thread_project_scopes
from connections.account_receipts import ReceiptFence, account_receipt
from connections.vendor_accounts import VENDORS, AccountError, get_account
from infra.database import write_transaction
from infra.write_effects import after_commit

log = logging.getLogger(__name__)

READY, LIMITED, SIGNED_OUT, UNCHECKED, NO_USABLE_MODEL = (
    "ready", "limited", "signed_out", "unchecked", "no_usable_model")
REFUSALS = ("not_ready", "limited", "no_usable_model", "unchecked", "stale", "not_permitted",
            "corsia_agent_started", "account_missing")
_NOT_DISCOVERED = ("account_model_not_discovered", "account_login_unverified")


class NotCorsiaAgent(LookupError):
    """The thread is missing, not this user's, or not the CORSIA agent."""


@dataclass(frozen=True, slots=True)
class RefreshObservation:
    """What one sign-in's own model refresh left, so a bind on it can tell a newer one ran.

    ``finished_at`` is read the moment that refresh returned: any later refresh of
    this login stamps its record after it, and any newer login changes the receipt.
    """

    receipt: ReceiptFence
    finished_at: str


@dataclass(slots=True)
class FirstRunBind:
    """A sign-in's first-run conductor, and its outcome once the login's own refresh and bind finished."""

    thread_id: str
    user_id: str
    outcome: str | None = None
    bind_reason: str | None = None

    def public(self) -> dict[str, Any]:
        # The first-run screen waits for a non-null outcome: the refresh and bind have finished.
        return {"outcome": self.outcome, "bind_reason": self.bind_reason}


def _vendor_rows(provider: str, *, db) -> list[dict]:
    """The vendor's catalog models the picker lists, each as its account-independent row."""
    from llm.model_status import harness_base_row

    models = [row[0] for row in db.execute(
        "SELECT litellm_model FROM models WHERE harness=? AND enabled=1 ORDER BY litellm_model", (provider,))]
    return [row for row in (harness_base_row(model, db=db) for model in models) if row is not None]


def account_state(account: dict, *, project_ids: list, db, rows: list[dict] | None = None) -> tuple[str, str | None]:
    """One account's first-run state, and when a limited one is back.

    Ready means at least one of its vendor's models passes the same per-account
    decision the picker and a turn use (``account_model_status``: lifecycle,
    project policy, this account's discovered models, then its usage). With none
    ready, a limit on any model reads limited; models never read for this login
    read unchecked; otherwise this plan offers no model CORSIA can run. ``rows``
    are the vendor's catalog rows when the caller already read them. The ambient
    Default has no managed account lifecycle, so its source's definitive
    needs-auth state is its signed-out state.
    """
    from connections.account_evidence import readiness_reason
    from connections.models import get_connection
    from connections.source_state import NEEDS_AUTH, row_state
    from llm.account_catalog import account_model_status
    from llm.model_status import USAGE_LIMITED

    if account["state"] == "signed_out" or (
            account["source_home"] is None
            and row_state(get_connection(account["provider"], db=db))["state"] == NEEDS_AUTH):
        return SIGNED_OUT, None
    decisions = [account_model_status(account, row, project_ids=project_ids, db=db)
                 for row in (_vendor_rows(account["provider"], db=db) if rows is None else rows)]
    if any(decision["status"] == "ready" for decision in decisions):
        return READY, None
    resets = [decision["usage"].get("resets_at") for decision in decisions
              if decision["status_reason"] == USAGE_LIMITED]
    if resets:
        known = sorted(value for value in resets if isinstance(value, str) and value)
        return LIMITED, known[0] if known else None
    if readiness_reason(account) in _NOT_DISCOVERED or not decisions or all(
            decision["status_reason"] == "connection_unavailable" for decision in decisions):
        return UNCHECKED, None
    return NO_USABLE_MODEL, None


def _after(stamp: object, moment: datetime) -> bool:
    return isinstance(stamp, str) and bool(stamp) and datetime.fromisoformat(stamp) > moment


def _stale(account: dict, observed: RefreshObservation) -> bool:
    """Whether a newer sign-in or refresh replaced the one this bind runs on."""
    from connections.account_evidence import discovery_record

    if account_receipt(account) != observed.receipt:
        return True
    discovery = discovery_record(account)["discovery"]
    finished = datetime.fromisoformat(observed.finished_at)
    failure = discovery.get("refresh_failure") or {}
    return _after(discovery.get("observed_at"), finished) or _after(failure.get("observed_at"), finished)


def _admission_refusal(reason: str) -> str:
    if reason == "account_removed":
        return "account_missing"
    if reason == "account_signed_out":
        return "not_ready"
    return "not_permitted"


def _refusal(thread: dict, account: dict | None, observed: RefreshObservation | None, *, db) -> str | None:
    """The first reason this account cannot be bound now, in the contract's order."""
    from connections.account_moves import movable_thread

    if account is None or account["state"] == "removed":
        return "account_missing"
    if account["provider"] not in VENDORS:
        return "not_permitted"
    if observed is not None and _stale(account, observed):
        return "stale"
    if str(thread["model"] or "").strip() or str(thread["pending_model"] or "").strip() or not movable_thread(
            thread["id"], db=db):
        return "corsia_agent_started"
    projects = thread_project_scopes(thread["id"], None, db=db)
    try:
        admit_account(account["provider"], account["id"], projects, execution=False, db=db)
    except AccountAdmissionError as refused:
        return _admission_refusal(refused.reason)
    state, _ = account_state(account, project_ids=projects, db=db)
    return {READY: None, SIGNED_OUT: "not_ready"}.get(state, state)


def _conductor(thread_id: str, user_id: str | None, *, db) -> dict:
    from infra.corsia_agent import get_corsia_agent

    row = db.execute("SELECT id,user_id,model,pending_model FROM threads WHERE id=?", (thread_id,)).fetchone()
    if row is None or row["user_id"] != user_id or get_corsia_agent(db=db) != thread_id:
        raise NotCorsiaAgent(thread_id)
    return dict(row)


def require_corsia_agent(thread_id: str, user_id: str, *, db=None) -> None:
    """Refuse a sign-in's ``bind_thread_id`` that is not this user's CORSIA agent."""
    from infra.database import get_db

    connection = db or get_db()
    try:
        _conductor(thread_id, user_id, db=connection)
    finally:
        if db is None:
            connection.close()


def bind_corsia_agent_account(thread_id: str, account_id: str, *, user_id: str,
                           observed: RefreshObservation | None = None, db=None) -> dict:
    """Bind the model-less CORSIA agent to ``account_id``; ``{bound, reason}``.

    ``observed`` is a sign-in's own refresh: with it, a newer sign-in or refresh of
    that account makes the bind stale. Without it (a person choosing an account
    they already have) the account's current model read decides. A move is
    recorded as the person's choice (``account_pinned``).
    """
    from connections.account_selection import rebind_idle_account
    from infra.state.ws import schedule_models_changed

    with write_transaction(db, scope="bind_corsia_agent_account") as connection:
        thread = _conductor(thread_id, user_id, db=connection)
        try:
            account = get_account(account_id, db=connection)
        except AccountError:
            account = None
        reason = _refusal(thread, account, observed, db=connection)
        if reason is not None:
            return {"bound": False, "reason": reason}
        assert account is not None
        # The one idle-rebind writer: a first binding when the vendor has none, else a move.
        rebind_idle_account(thread_id, account_id, user_id=user_id, provider=account["provider"], db=connection)
        after_commit(connection, schedule_models_changed)
        return {"bound": True, "reason": None}


async def finish_signin(operation: Any, receipt: ReceiptFence | None, binary: str) -> None:
    """Read a completed sign-in's models once, then record its first-run outcome.

    ``operation`` is the sign-in record. Only a new inference-vendor login with a
    ``receipt`` is read; another account's repeat or a non-inference CLI is not.
    Open screens hear ``models_updated`` for every completion: a ready refresh and
    a bind each send it when they commit, so only the others send it here.
    """
    from infra.db_async import run_in_db
    from infra.state.ws import notify_models_changed

    reads = operation.provider in VENDORS and operation.duplicate_of is None and receipt is not None
    refresh, observed = await _refresh(operation, receipt, binary) if reads else ("skipped", None)
    bind = operation.bind
    if bind is not None:
        try:
            bind.outcome, bind.bind_reason = await run_in_db(signin_outcome, operation.account_id, refresh,
                                                             observed, bind=bind)
        except Exception as exc:  # noqa: BLE001 - a poller waits on this outcome, so it must always land
            log.warning("account %s sign-in outcome failed: %s", operation.account_id, type(exc).__name__)
            bind.outcome, bind.bind_reason = UNCHECKED, None
    if bind is not None and refresh == READY:
        from onboarding.first_chat_switch import switch_when_ready

        await switch_when_ready(operation.provider, f"{operation.account_id}:{operation.revision}",
                                user_id=bind.user_id, account_id=operation.account_id)
    if refresh != READY:
        notify_models_changed()


async def _refresh(operation: Any, receipt: ReceiptFence, binary: str) -> tuple[str, RefreshObservation | None]:
    """Read the new login's models once; how that ended, and what a ready read left."""
    from connections import harness_models
    from infra.time import utc_now_iso

    try:
        result = await harness_models.refresh_provider(operation.provider, reason="account-signin",
            account_id=operation.account_id, expected_receipt=receipt, binary=binary)
    except Exception as exc:  # noqa: BLE001 - a failed read leaves the sign-in completed and unchecked
        log.warning("account %s discovery failed: %s", operation.account_id, type(exc).__name__)
        return "failed", None
    # Taken before any await: a later refresh of this login is stamped after it.
    observed = RefreshObservation(receipt, utc_now_iso())
    if getattr(result, "cause", None) == "account_refresh_superseded":
        return "superseded", observed
    return str(getattr(result, "status", "failed")), observed


def signin_outcome(account_id: str, refresh: str, observed: RefreshObservation | None, *,
                   bind: FirstRunBind) -> tuple[str, str | None]:
    """A first-run sign-in's ``(outcome, bind_reason)`` once its own model refresh is done.

    ``refresh`` is how that refresh ended: ``ready``, ``failed``, ``superseded`` (a
    newer login replaced it) or ``skipped`` (it never ran); ``observed`` is what a
    ready one left. The conductor is bound only on a ready read of a Ready account.
    A refused bind reads ``bind_refused`` with its reason, except that a limited,
    unusable or unchecked account reads as that state.
    """
    if refresh == "superseded":
        return "bind_refused", "stale"
    if refresh != READY or observed is None:
        return UNCHECKED, None
    try:
        result = bind_corsia_agent_account(bind.thread_id, account_id, user_id=bind.user_id, observed=observed)
    except NotCorsiaAgent:
        # The thread stopped being this user's CORSIA agent while the person signed in.
        return "bind_refused", "corsia_agent_started"
    if result["bound"]:
        return READY, None
    if result["reason"] in (LIMITED, NO_USABLE_MODEL, UNCHECKED):
        return result["reason"], None
    return "bind_refused", result["reason"]


def source_accounts(provider: str) -> list[dict]:
    """The first-run card's account lines for one vendor: the picker's account rows, in order."""
    return source_account_view(provider)[0]


def _lost_binding(provider: str, bound, labels: list[dict], project, *, db) -> dict | None:
    """The conductor's binding when its account was removed or turned off for this project."""
    from connections.account_policy import account_reason
    from connections.account_profiles import account_label

    if bound is None:
        return None
    account = next((row for row in labels if row["id"] == str(bound["account_id"])), None)
    if account is None:
        return None
    if account["state"] == "removed":
        reason = "removed"
    elif account_reason(account, provider=provider, project_ids=[project], execution=False,
                        db=db) == "account_project_denied":
        reason = "turned_off"
    else:
        return None
    return {"label": account_label(account, rows=labels)["text"], "reason": reason}


def source_account_view(provider: str) -> tuple[list[dict], dict | None]:
    """One vendor's first-run account lines and its conductor's lost binding.

    Lines: membership is the picker's: the accounts the CORSIA agent's scope
    permits (never a removed one), and none when the vendor lists no model.
    ``corsia_agent_binding`` reads only the conductor's stored binding for the vendor.

    Lost binding: ``{label, reason}`` when that stored binding names an account that
    was removed (``removed``) or that this project turned off (``turned_off``), the
    two the lines skip; otherwise ``None``. ``label`` is the account's person-facing label.
    """
    from connections.account_policy import permitted_accounts, thread_scope
    from connections.account_profiles import account_label
    from connections.vendor_accounts import list_accounts
    from infra.corsia_agent import get_corsia_agent
    from infra.database import get_db

    db = get_db()
    try:
        conductor = get_corsia_agent(db=db)
        project = thread_scope(conductor, db=db)["project_id"] if conductor else None
        bound = db.execute("SELECT account_id FROM thread_vendor_accounts WHERE thread_id=? AND provider=?",
                           (conductor, provider)).fetchone() if conductor else None
        labels = list_accounts(provider=provider, include_removed=True, db=db)
        lost = _lost_binding(provider, bound, labels, project, db=db)
        rows = _vendor_rows(provider, db=db)  # read once per vendor, whatever its account count
        if not rows:
            return [], lost
        lines = []
        for account in permitted_accounts(provider, project_ids=[project], db=db):
            if account["state"] == "removed":
                continue
            label = account_label(account, rows=labels)
            state, resets_at = account_state(account, project_ids=[project], db=db, rows=rows)
            lines.append({"account_id": account["id"], "label": label["text"],
                          "label_verified": not (label["email"] is None and label["custom"]),
                          "identified": label["identified"],
                          "state": state, "resets_at": resets_at,
                          "corsia_agent_binding": bound is not None and str(bound["account_id"]) == account["id"]})
        return lines, lost
    finally:
        db.close()
