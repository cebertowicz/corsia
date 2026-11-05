"""Authenticated subscription metadata, ordered policy and explicit binding controls."""
from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field, StrictBool, StrictInt

from connections import account_controls
from connections import vendor_accounts as accounts
from connections.account_policy import public_account
from connections.account_profiles import account_label, profile_for
from connections.account_selection import rebind_idle_account
from connections.corsia_agent_account import (
    FirstRunBind,
    NotCorsiaAgent,
    accounts_without_usable_model,
    bind_corsia_agent_account,
    require_corsia_agent,
)

from infra.auth import require_user
from infra.database import get_db
from infra.db_async import run_in_db, run_read

router = APIRouter(prefix="/api", tags=["vendor-accounts"], dependencies=[Depends(require_user)])


class AddAccount(BaseModel):
    provider: str
    name: str
    identity_hint: dict | None = None


class Revision(BaseModel):
    expected_revision: StrictInt = Field(ge=0)


class RenameAccount(Revision):
    name: str



class AccountUsage(BaseModel):
    allow_overage: StrictBool


class AccountPrice(BaseModel):
    # Both None clears the price: the account then reads as not recorded, never as free.
    monthly_price_cents: StrictInt | None
    charge_day: StrictInt | None
    expected_billing_revision: StrictInt = Field(ge=0)


class AccountOverage(BaseModel):
    amount_cents: StrictInt
    month: str
    expected_billing_revision: StrictInt = Field(ge=0)


class AccountBinding(BaseModel):
    account_id: str


class SigninStart(BaseModel):
    # The first-run screen's conductor, bound once the sign-in's own model refresh reads Ready.
    bind_thread_id: str | None = None


def _safe(account):
    facts = profile_for(account["provider"]).identity_facts
    identity = {key: value for key, value in account["identity"].items()
                if key in facts and key != "account_fp" and isinstance(value, str)}
    return public_account(account) | {"identity": identity, "upgrade_reason": account["identity"].get("upgrade_reason")}


def _allow_overage(account) -> bool:
    """The account's own 'Spend past usage limit' switch."""
    from connections.harness_usage import allows_overage

    return allows_overage(account["provider"], account["id"])


async def _call(fn, *args, **kwargs):
    return await _account_errors(run_in_db(fn, *args, **kwargs))


async def _read(fn, *args, **kwargs):
    """``_call`` for a job that never writes, on the read-only read lane."""
    return await _account_errors(run_read(fn, *args, **kwargs))


async def _account_errors(job):
    try:
        return await job
    except accounts.AccountError as error:
        raise HTTPException(409, str(error)) from error


def _usage(account) -> dict:
    """The account's own usage, delivered once here and never repeated on a model row."""
    from connections.harness_usage import account_usage, stored_usage
    from connections.harness_usage_check import is_checking

    return account_usage(
        stored_usage(provider=account["provider"], account_id=account["id"]), provider=account["provider"],
    ) | {
        "checking": is_checking(account["id"])}


def _billing_views() -> dict[str, dict]:
    """Every account's price in effect today and billing revision, read from the one fact store."""
    from connections.subscription_billing import account_billing

    db = get_db()
    try:
        return account_billing(db)
    finally:
        db.close()


async def _no_usable_model(providers, rows=None) -> set[str]:
    """The accounts, across these vendors, that signed in but have no model CORSIA can run."""
    wanted = {provider for provider in providers if provider in accounts.VENDORS}
    return await run_read(accounts_without_usable_model, wanted, rows) if wanted else set()


async def _shown(account, rows=None, numbered_rows=None, billing_views=None, no_model=None):
    """The account as the API returns it, with its person-facing label."""
    if billing_views is None:
        billing_views = await run_in_db(_billing_views)
    if no_model is None:
        no_model = await _no_usable_model([account["provider"]])
    if rows is None:
        rows = await _read(accounts.list_accounts, provider=account["provider"])
    if numbered_rows is None:
        numbered_rows = await _read(accounts.list_accounts, provider=account["provider"], include_removed=True)
    return _safe(account) | {"duplicate_of": await _read(accounts.duplicate_of, account, rows=rows),
                             "allow_overage": await run_read(_allow_overage, account),
                             "no_usable_model": account["id"] in no_model,
                             "label": account_label(account, rows=numbered_rows),
                             **billing_views.get(account["id"], {"billing": None, "billing_revision": 0}),
                             "usage": await run_read(_usage, account)}


@router.get("/vendor-accounts")
async def list_vendor_accounts(provider: str | None = None, include_removed: bool = False):
    rows = await _read(accounts.list_accounts, provider=provider, include_removed=include_removed)
    numbered_rows = rows if include_removed else await _read(
        accounts.list_accounts, provider=provider, include_removed=True)
    billing_views = await run_read(_billing_views)
    no_model = await _no_usable_model((row["provider"] for row in rows), rows)
    return {"accounts": [await _shown(row, rows, numbered_rows, billing_views, no_model) for row in rows]}


@router.post("/vendor-accounts")
async def add_vendor_account(body: AddAccount):
    return await _shown(await _call(accounts.add_account, body.provider, body.name,
                                    identity_hint=body.identity_hint))


@router.patch("/vendor-accounts/{account_id}")
async def rename_vendor_account(account_id: str, body: RenameAccount):
    return await _shown(
        await _call(accounts.rename_account, account_id, body.name, expected_revision=body.expected_revision))


@router.put("/vendor-accounts/{account_id}/default")
async def set_default_vendor_account(account_id: str, body: Revision):
    """Choose a provider's workspace default; refused where the provider fixes its own."""
    return await _shown(await _call(accounts.set_default_account, account_id,
                                    expected_revision=body.expected_revision))


@router.delete("/vendor-accounts/{account_id}/default")
async def clear_default_vendor_account(account_id: str, body: Revision):
    """Leave a provider with no workspace default; refused where the provider fixes its own."""
    return await _shown(await _call(accounts.clear_default_account, account_id,
                                    expected_revision=body.expected_revision))


def _set_allow_overage(account_id, allow):
    from connections.harness_usage import set_allow_overage

    account = accounts.get_account(account_id)
    if account["state"] == "removed":
        raise accounts.AccountError("account_has_no_plan_limit")
    set_allow_overage(account["provider"], account_id, allow)
    return accounts.get_account(account_id)


async def _billing_write(write, account_id: str, **fields):
    """Run one billing write and return the account as the API shows it.

    The write is guarded by the account's billing revision alone and never bumps the
    account revision, so a sign-in that began earlier still commits.
    """
    from connections.subscription_billing import BillingFactValidationError

    try:
        await run_in_db(write, account_id, **fields)
    except BillingFactValidationError as error:
        raise HTTPException(422, str(error)) from error
    except accounts.AccountError as error:
        # `code` is the contract clients branch on; `detail` is only for people.
        return JSONResponse(status_code=409, content={"detail": str(error), "code": error.code})
    return await _shown(await _call(accounts.get_account, account_id))


@router.put("/vendor-accounts/{account_id}/billing")
async def set_vendor_account_billing(account_id: str, body: AccountPrice):
    """Record this account's monthly price and charge day, or clear both."""
    from connections.subscription_billing import set_account_price

    return await _billing_write(
        set_account_price, account_id, monthly_price_cents=body.monthly_price_cents,
        charge_day=body.charge_day, expected_billing_revision=body.expected_billing_revision)


@router.post("/vendor-accounts/{account_id}/overage")
async def record_vendor_account_overage(account_id: str, body: AccountOverage):
    """Record what this account paid past its plan limit for one calendar month."""
    from connections.subscription_billing import record_overage

    return await _billing_write(
        record_overage, account_id, amount_cents=body.amount_cents, month=body.month,
        expected_billing_revision=body.expected_billing_revision)


@router.put("/vendor-accounts/{account_id}/usage")
async def update_vendor_account_usage(account_id: str, body: AccountUsage):
    """Set this account's 'Spend past usage limit' switch (off by default)."""
    from infra.state.ws import notify_models_changed

    account = await _call(_set_allow_overage, account_id, body.allow_overage)
    notify_models_changed()
    return await _shown(account)


async def _control(fn, *args, **kwargs):
    try:
        return await fn(*args, **kwargs)
    except accounts.AccountError as error:
        raise HTTPException(409, str(error)) from error


async def _left(result):
    account, cleanup = result
    return await _shown(account) | {"auth_cleanup_status": cleanup["status"], "auth_cleanup_cause": cleanup["cause"]}


@router.post("/vendor-accounts/{account_id}/signin")
async def start_vendor_signin(account_id: str, body: SigninStart | None = None,
                              user: dict = Depends(require_user)):
    bind = None
    if body is not None and body.bind_thread_id is not None:
        await _corsia_agent_call(require_corsia_agent, body.bind_thread_id, user["id"])
        bind = FirstRunBind(body.bind_thread_id, user["id"])
    return await _control(account_controls.start_signin, account_id, bind=bind)


@router.post("/vendor-accounts/{account_id}/signin/check")
async def check_vendor_signin(account_id: str):
    return await _control(account_controls.check_signin, account_id)


@router.delete("/vendor-accounts/{account_id}/signin")
async def cancel_vendor_signin(account_id: str):
    return await _control(account_controls.cancel_signin, account_id)


@router.post("/vendor-accounts/{account_id}/verify")
async def verify_vendor_account(account_id: str):
    return await _control(account_controls.verify, account_id)


@router.post("/vendor-accounts/{account_id}/signout")
async def signout_vendor_account(account_id: str, body: Revision):
    return await _left(await _control(account_controls.sign_out, account_id, expected_revision=body.expected_revision))


@router.delete("/vendor-accounts/{account_id}")
async def remove_vendor_account(account_id: str, body: Revision):
    return await _left(await _control(account_controls.remove, account_id, expected_revision=body.expected_revision))



def _thread_accounts(thread_id, user_id):
    db = get_db()
    try:
        if db.execute("SELECT 1 FROM threads WHERE id=? AND user_id=?", (thread_id, user_id)).fetchone() is None:
            raise HTTPException(404, "Thread not found")
        rows = db.execute("SELECT b.provider,b.account_id,h.reason FROM thread_vendor_accounts b "
                          "LEFT JOIN thread_vendor_account_holds h ON h.thread_id=b.thread_id AND h.provider=b.provider "
                          "WHERE b.thread_id=? ORDER BY b.provider", (thread_id,)).fetchall()
        return {"accounts": [_safe(accounts.get_account(row["account_id"], db=db)) | {"hold_reason": row["reason"]}
                             for row in rows]}
    finally:
        db.close()


@router.get("/threads/{thread_id}/vendor-account")
async def get_thread_accounts(thread_id: str, user: dict = Depends(require_user)):
    return await run_in_db(_thread_accounts, thread_id, user["id"])


@router.put("/threads/{thread_id}/vendor-account")
async def put_thread_account(thread_id: str, body: AccountBinding, user: dict = Depends(require_user)):
    # commit_move owns the single account-move publication path and durable receipt.
    return await _call(rebind_idle_account, thread_id, body.account_id, user_id=user["id"])


async def _corsia_agent_call(fn, *args, **kwargs):
    try:
        return await run_in_db(fn, *args, **kwargs)
    except NotCorsiaAgent as error:
        raise HTTPException(404, "CORSIA agent thread not found") from error


@router.post("/threads/{thread_id}/corsia-agent-account")
async def bind_corsia_agent_thread_account(thread_id: str, body: AccountBinding, user: dict = Depends(require_user)):
    """Bind the model-less CORSIA agent to a Ready account: ``{bound, reason}``, writing nothing when refused."""
    return await _corsia_agent_call(bind_corsia_agent_account, thread_id, body.account_id, user_id=user["id"])
