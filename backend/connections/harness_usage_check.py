"""Usage-only recovery checks for CLI harness accounts.

The model picker triggers these checks when it opens, and a Turn that ends on
the expired-login text awaits one through ``check_login``. A Claude account is
checked through its own usage-only control read, with no catalog discovery or
model turn, in its captured source home. A Codex account is read by its own
account refresh, in its own home and login. Each account has one in-memory check
at a time; scheduled checks start no more than once per five minutes, and a
login check skips that throttle.
"""

from __future__ import annotations

import asyncio
import logging
import time
from datetime import datetime, timezone

from connections import harness_usage
from connections.usage_windows import parse_time
from connections.vendor_accounts import VENDORS
from infra.db_async import run_in_db

log = logging.getLogger(__name__)
_RECHECK_SECONDS = 300.0
#: A Turn's end waits on ``check_login``: past the control read's own 30 s bound
#: (``claude_discovery.run_claude_control``), so a slow read answers rather than
#: times out, and inside the 35 s live-close bound
#: (``cc_runtime_state._CLOSE_LIVE_TIMEOUT_S``) that a Stop during that wait runs under.
_LOGIN_CHECK_SECONDS = 32.0
#: Keyed by account id; each read answers ``(verdict, the login it read)``.
_CHECKS: dict[str, asyncio.Task[tuple[str, object]]] = {}
_LAST_STARTED: dict[str, float] = {}
#: Accounts signed in while a read for the prior login was running; read again once it ends.
_NEW_LOGIN_PENDING: set[str] = set()


def is_checking(account_id: str) -> bool:
    """Whether this process is currently rechecking one harness account."""
    task = _CHECKS.get(account_id)
    return task is not None and not task.done()


def limited_accounts(*, db=None) -> list[tuple[str, str]]:
    """(provider, account id) of every current login that needs the canonical control read."""
    from connections.account_receipts import account_receipt
    from connections.vendor_accounts import list_accounts
    from infra.database import get_db

    connection = db or get_db()
    try:
        limited = []
        for account in list_accounts(db=connection):
            if account['provider'] not in VENDORS or account_receipt(account) is None:
                continue
            usage = harness_usage.stored_usage(provider=account['provider'], account_id=account['id'], db=connection)
            projection = harness_usage.account_usage(usage, provider=account['provider'])
            reset_passed = any(
                isinstance(window, dict)
                and (reset := parse_time(window.get("resets_at"))) is not None
                and reset <= datetime.now(timezone.utc)
                for window in projection.get("windows", ())
            )
            if not projection.get("usable", True) or reset_passed:
                limited.append((account['provider'], account['id']))
        return limited
    finally:
        if db is None:
            connection.close()


async def schedule_limited_usage_rechecks() -> None:
    """Schedule one control read per limited or expired visible account when the picker opens."""
    for provider, account_id in await run_in_db(limited_accounts):
        _schedule(provider, account_id)


def schedule_account_check(provider: str, account_id: str, *, new_login: bool = False) -> None:
    """Request the account's canonical bounded control read from any thread.

    The account-wide single-flight guard applies to every control read, and the
    five-minute throttle to every scheduled one, except that a new login's first
    read is never throttled by the prior login's: its stored readings were just
    marked stale. ``check_login`` joins the single flight but skips the throttle.
    """
    import infra.app_loop as app_loop

    async def _start() -> None:
        if new_login:
            _LAST_STARTED.pop(account_id, None)
            if is_checking(account_id):
                # The running read is fenced to the prior login and cannot settle this one.
                _NEW_LOGIN_PENDING.add(account_id)
                return
        _schedule(provider, account_id)

    try:
        app_loop.submit(_start())
    except app_loop.AppLoopUnavailable:
        log.warning("%s usage read for account %s not scheduled: no live event loop", provider, account_id)


def _schedule(provider: str, account_id: str) -> None:
    if is_checking(account_id):
        return
    now = time.monotonic()
    if now - _LAST_STARTED.get(account_id, float("-inf")) < _RECHECK_SECONDS:
        return
    _LAST_STARTED[account_id] = now
    _CHECKS[account_id] = asyncio.create_task(_check(provider, account_id))


def _capture(provider: str, account_id: str, *, db=None):
    """Capture the current account context once before a bounded usage read."""
    from connections.account_discovery_types import capture_discovery_context

    return capture_discovery_context(provider, account_id=account_id, db=db)


def _login_check_task(provider: str, account_id: str) -> asyncio.Task:
    task = _CHECKS.get(account_id)
    if task is None or task.done():
        _LAST_STARTED[account_id] = time.monotonic()
        task = _CHECKS[account_id] = asyncio.create_task(_check(provider, account_id))
    return task


def schedule_login_check(provider: str, receipt) -> None:
    """Submit an explicit receipt check on the ingress loop without waiting on control I/O."""
    from ws.ingress import track_background_task

    # Establish the canonical single flight now; the tracked waiter owns its bound.
    task = _login_check_task(provider, receipt.account_id)
    track_background_task(asyncio.create_task(_wait_for_login(provider, receipt, task)))


async def check_login(provider: str, receipt) -> str:
    """The control read of the login ``receipt`` names: ``pass`` when it answered, else ``fail`` or ``error``.

    Joins the read already running for the account, or starts one now past the
    five-minute throttle, so every caller shares one control process per login.
    A read of another login says nothing about this one, so the next read is
    joined or started; when that one reads another login too, the receipt's
    login was replaced or signed out, and the answer is ``fail``.
    Waits at most ``_LOGIN_CHECK_SECONDS`` in all; a read still running then finishes on its own.
    """
    return await _wait_for_login(provider, receipt, _login_check_task(provider, receipt.account_id))


async def _wait_for_login(provider: str, receipt, task: asyncio.Task) -> str:
    account_id = receipt.account_id
    try:
        async with asyncio.timeout(_LOGIN_CHECK_SECONDS):
            verdict, read_for = await asyncio.shield(task)
            if read_for == receipt:
                return verdict
            verdict, read_for = await asyncio.shield(_login_check_task(provider, account_id))
            return verdict if read_for == receipt else "fail"
    except TimeoutError:
        log.warning("%s login check timed out for account %s", provider, account_id)
        return "error"


async def _read(provider: str, account_id: str) -> tuple[str, object]:
    """``(verdict, the login receipt it read)``; the receipt is None when no login was read."""
    receipt = None
    try:
        context = await run_in_db(_capture, provider, account_id)
        if context is None or context.receipt is None:
            return "fail", None  # No current login to read for: the next sign-in schedules its own check.
        receipt = context.receipt
        if provider != "claude-code":
            return ("pass" if await _check_codex(provider, receipt) else "fail"), receipt
        binary = await _binary_for(provider)
        if binary is None:
            await _check_failed(provider, receipt, "Harness connection is unavailable")
            return "error", receipt
        verdict = await _check_claude(binary, receipt, context=context)
        # Only plan usage read through the login proves it answered; an unknown read proves nothing.
        return ("pass" if verdict["verdict"] in ("allowed", "limited") else "fail"), receipt
    except Exception as exc:
        log.warning("%s usage recheck failed for account %s: %s", provider, account_id, exc)
        await _check_failed(provider, receipt, f"{provider} usage read failed: {exc}")
        return "error", receipt


async def _check(provider: str, account_id: str) -> tuple[str, object]:
    try:
        return await _read(provider, account_id)
    finally:
        _CHECKS.pop(account_id, None)
        if account_id in _NEW_LOGIN_PENDING:
            _NEW_LOGIN_PENDING.discard(account_id)
            _LAST_STARTED.pop(account_id, None)
            _schedule(provider, account_id)
        from infra.state.ws import notify_models_changed

        notify_models_changed()


async def _binary_for(provider: str) -> str | None:
    from connections import harness_models

    binary, unavailable = await harness_models._db_call(
        harness_models._provider_binary, provider,
    )
    return None if unavailable else binary


async def _check_failed(provider: str, receipt, error: str) -> None:
    await run_in_db(harness_usage.record_check_failed, error=error, provider=provider, receipt=receipt)


async def _check_codex(provider: str, receipt) -> bool:
    """A Codex account's usage is read by its own refresh, in its own home and login; whether it read."""
    from connections import harness_models

    result = await harness_models.refresh_provider(provider, reason="usage-recheck", account_id=receipt.account_id,
                                                   expected_receipt=receipt)
    # A refresh that never read usage leaves the last verdict standing and says so, as any failed check does.
    if isinstance(result, dict):
        failed, cause = not result.get("ok"), result.get("error")
    else:
        failed, cause = result.status != "ready", result.cause
    if failed and cause != "account_refresh_superseded":
        await _check_failed(provider, receipt, f"{provider} usage read failed: {cause or 'refresh did not finish'}")
    return not failed


async def _check_claude(binary: str, receipt, *, context=None) -> dict:
    from connections import claude_usage
    from connections.claude_discovery import _control_responses, run_claude_control

    frames = [
        {"type": "control_request", "request_id": "corsia-usage",
         "request": {"subtype": "get_usage", "skip_behaviors": True}},
        {"type": "control_request", "request_id": "corsia-usage-end",
         "request": {"subtype": "end_session", "reason": "usage_check_complete"}},
    ]
    stdout = await run_claude_control(
        binary, frames, operation="get_usage",
        **({'context': context} if context is not None else {}),
    )
    response = _control_responses(stdout).get("corsia-usage")
    report = response.get("response") if response and response.get("subtype") == "success" else None
    return await run_in_db(claude_usage.apply_usage_response, report, receipt=receipt)
