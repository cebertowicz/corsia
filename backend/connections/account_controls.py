"""Vendor-owned sign-in, status and sign-out for an added account's own home.

CORSIA retains only a hash when comparing Codex logins; each vendor CLI writes and
removes its own for the home it is given. Sign-in is the vendor's browser
route, held in memory until the vendor process exits (or, for Claude Code, its status
reads signed in), the person cancels or the five-minute wait runs out; a restart drops
it and the account stays signed out. Only the vendor's status command decides whether
an account is signed in. Default identity is read only
at completion and verify; its login is never changed here.
"""
from __future__ import annotations

import asyncio
import contextlib
import json
import logging
from typing import Any

from connections import account_cleanup, account_login_wait, account_signin_prompt
from connections.account_verify_refresh import custom_account, refresh_models, signin_recheck
from connections.account_identity import codex_identity, refresh_default_identity
from connections.account_lifecycle import change_state
from connections.account_profiles import profile_for
from connections.account_receipts import account_receipt
from connections.account_signin import SignIn, failure_reference
from connections.account_leftovers import record_failed_stop, remove_if_leftover, settle_signin
from connections.account_evidence import readiness_reason
from connections.account_signin_commit import SameAccount, commit_signed_in
from connections.corsia_agent_account import FirstRunBind, finish_signin
from connections.harness_process import killed_on_exit
from connections.vendor_accounts import VENDORS, AccountError, duplicate_of, get_account
from infra.db_async import run_in_db


log = logging.getLogger(__name__)
# Bounds the short status and logout commands only; a held sign-in has its own, longer bound.
_COMMAND_SECONDS = 30.0
# The vendor CLIs wait on the browser callback with no limit of their own (Claude Code's
# `auth login` waits on the callback or a pasted code; Codex's on its callback port), and a
# browser that stops on a vendor page never calls back. A person who signs in finishes well
# inside this.
_SIGNIN_WAIT_SECONDS = 300.0



class _SigninWaitExpired(Exception):
    """The held vendor login stayed open past ``_SIGNIN_WAIT_SECONDS``."""


_SIGNINS: dict[str, SignIn] = {}



async def _binary(provider: str) -> str | None:
    # Found whatever the source row says: a signed-out CLI reads needs_auth, and that is
    # exactly when its sign-in has to start.
    from connections import resolve_builtin_binary
    return (await run_in_db(resolve_builtin_binary, provider))[1]


async def _spawn(provider: str, home: str, binary: str, action: str) -> asyncio.subprocess.Process:
    """Start one vendor command for the stored home string, verbatim, with stdin closed.

    Login runs as it was observed to work: both output streams into the one
    pipe, which is read and dropped. Status keeps standard error out of the
    report it parses.
    """
    profile = profile_for(provider)
    env = profile.clean_env()
    env[profile.home_variable] = home
    return await asyncio.create_subprocess_exec(
        binary, *profile.commands[action], env=env, stdin=asyncio.subprocess.DEVNULL,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.STDOUT if action == "login" else asyncio.subprocess.DEVNULL,
    )


async def _run(provider: str, home: str, binary: str, action: str) -> tuple[int, bytes]:
    """Run one bounded vendor command; its output never leaves this module."""
    process = await _spawn(provider, home, binary, action)
    async with killed_on_exit(process):
        async with asyncio.timeout(_COMMAND_SECONDS):
            stdout, _ = await process.communicate()
    return int(process.returncode or 0), stdout


async def _vendor_status(provider: str, home: str, binary: str) -> tuple[bool | None, dict[str, str], str | None]:
    """Whether the vendor says this home is signed in, its identity facts, and a cause when unknown."""
    try:
        code, stdout = await _run(provider, home, binary, "status")
    except (OSError, TimeoutError):
        return None, {}, "vendor_status_unavailable"
    profile = profile_for(provider)
    if profile.parse_status is not None:
        signed_in, facts = profile.parse_status(code, stdout)
        return signed_in, facts, None if signed_in is not None else "vendor_status_unreadable"
    if code == 1:
        return False, {}, None
    if code != 0:
        return None, {}, "vendor_status_failed"
    if not profile.status_report_keys:
        # A vendor that names no facts in its status report keeps them in its auth file.
        return True, await run_in_db(codex_identity, home), None
    try:
        report = json.loads(stdout)
    except ValueError:
        report = None
    if not isinstance(report, dict) or not isinstance(report.get("loggedIn"), bool):
        return None, {}, "vendor_status_unreadable"
    if not report["loggedIn"]:
        return False, {}, None
    return True, profile.facts_from_status(report), None


def _status_poll(operation: SignIn, home: str, binary: str):
    """The vendor-status read that can end a held login early, for a vendor whose profile allows it."""
    if not profile_for(operation.provider).status_ends_login:
        return None

    async def read() -> bool | None:
        return (await _vendor_status(operation.provider, home, binary))[0]

    return read


async def _hold(operation: SignIn, home: str, binary: str) -> None:
    """Wait for the vendor login to exit by itself, then let the vendor's status decide.

    A login still open after ``_SIGNIN_WAIT_SECONDS`` is killed and fails as ``signin_timed_out``.
    """
    process = operation.process
    assert process is not None and process.stdout is not None
    try:
        # Read and drop the output so the vendor never blocks on a full pipe; a
        # descendant holding the pipe open must not delay the exit either.
        drain = asyncio.ensure_future(account_signin_prompt.discard(process.stdout, operation))
        async with killed_on_exit(process):
            try:
                async with asyncio.timeout(_SIGNIN_WAIT_SECONDS):
                    await account_login_wait.login_done(process, _status_poll(operation, home, binary))
            except TimeoutError:
                raise _SigninWaitExpired from None
            finally:
                # Settle the reader before anything else reads the pipe to reap the process.
                drain.cancel()
                await asyncio.gather(drain, return_exceptions=True)
        # A login stopped here had stored its credentials: the status read below decides, not its exit code.
        if process.returncode is None:
            await account_login_wait.stop_login(process)
        elif process.returncode != 0:
            operation.status, operation.cause = "failed", "vendor_login_failed"
            return
        signed_in, identity, cause = await _vendor_status(operation.provider, home, binary)
        if signed_in is not True:
            operation.status, operation.cause = "failed", cause or "vendor_not_signed_in"
            return
        refusal = profile_for(operation.provider).signin_refusal
        if refusal is not None and (refused := await run_in_db(refusal, home, identity)) is not None:
            # The login stands in the home until the vendor's own logout removes it.
            await account_cleanup.cleanup(await run_in_db(get_account, operation.account_id))
            operation.status, operation.cause = "failed", refused
            return
        if operation.provider in VENDORS:
            await refresh_default_identity(operation.provider)
        try:
            operation.duplicate_of, receipt = await run_in_db(
                commit_signed_in, operation.account_id, operation.revision, identity)
        except SameAccount as repeat:
            from connections.account_signin_retry import retry_unchecked
            await retry_unchecked(operation, str(repeat), binary, account_cleanup.cleanup)
            return
        except AccountError:
            operation.status, operation.cause = "superseded", "account_changed"
            return
        operation.status, operation.cause = "completed", None
        await finish_signin(operation, receipt, binary)
    except _SigninWaitExpired:  # the vendor process was killed on the way out
        operation.status, operation.cause = "failed", "signin_timed_out"
    except Exception as exc:  # noqa: BLE001 - nobody may be left waiting on a sign-in nothing is waiting on
        log.warning("account %s sign-in check failed: %s", operation.account_id, type(exc).__name__)
        if operation.status == "cancelled":
            await record_failed_stop(operation)
        if operation.status == "waiting" or (operation.status == "completed" and operation.bind and operation.bind.outcome is None):
            operation.status, operation.cause = "failed", "signin_check_failed"
    finally:
        await settle_signin(operation)


async def start_signin(account_id: str, *, bind: FirstRunBind | None = None) -> dict[str, Any]:
    """Begin, or return the live, vendor browser sign-in for an added account."""
    account = await custom_account(account_id)
    if account["state"] == "signed_in":
        if account["provider"] not in VENDORS:
            raise AccountError("account is already signed in")
        result = await signin_recheck(account_id, account["provider"], lambda selected: verify(selected, report_refresh=True))
        if result is not None:
            return result
        account = await custom_account(account_id)
    provider = account["provider"]
    binary = await _binary(provider)
    if binary is None:
        cause = profile_for(provider).missing_cause
        return {"account_id": account_id, "route": None, "status": "unavailable", "cause": cause,
                "ref": failure_reference(provider, cause)}
    # No await between these checks and the record, so two starts cannot both pass.
    current = _SIGNINS.get(account_id)
    if current is not None and current.held:
        current.bind = bind or current.bind
        return current.public()
    # One sign-in per vendor. Codex's browser callback listens on one fixed port: one Codex
    # sign-in per host. Two Claude sign-ins at once are unobserved.
    holder = next((other for other in _SIGNINS.values() if other.provider == provider and other.held), None)
    if holder is not None:
        return {"account_id": account_id, "route": "browser", "status": "refused",
                "cause": profile_for(provider).in_progress_cause, "held_by": holder.account_id}
    # A finished record for this account is replaced here.
    operation = _SIGNINS[account_id] = SignIn(account_id, provider, account["revision"], bind=bind)
    if profile_for(provider).logout_before_login:
        # Nothing else may wait between the record and the login, so this one waits here.
        try:
            await _run(provider, account["source_home"], binary, "logout")
        except (OSError, TimeoutError):
            operation.status, operation.cause = "failed", "vendor_logout_unavailable"
            await settle_signin(operation)
            return operation.public()
    if operation.status != "waiting":  # cancelled while the stale login was removed
        return operation.public()
    try:
        operation.process = await _spawn(provider, account["source_home"], binary, "login")
    except OSError:
        operation.status, operation.cause = "failed", "vendor_login_unavailable"
        await settle_signin(operation)
        return operation.public()
    if operation.status != "waiting":  # cancelled while the vendor process was starting
        await _stop(operation)
        return operation.public()
    operation.task = asyncio.create_task(_hold(operation, account["source_home"], binary))
    return operation.public()


async def check_signin(account_id: str) -> dict[str, Any]:
    """Report one sign-in from its in-process record; a finished one is reported on every check."""
    operation = _SIGNINS.get(account_id)
    if operation is None:
        raise AccountError("no sign-in is in progress for this account")
    return operation.public()


async def wait_for_prompt(account_id: str, *, timeout: float) -> dict[str, Any]:
    """Report one sign-in once the vendor printed its prompt, it finished, or ``timeout`` passed."""
    operation = _SIGNINS.get(account_id)
    if operation is None:
        raise AccountError("no sign-in is in progress for this account")
    if operation.held:
        await account_signin_prompt.wait(operation, timeout)
    return operation.public()


async def _stop(operation: SignIn) -> None:
    if operation.task is not None and not operation.task.done():
        operation.task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await operation.task
    # A task cancelled before its first step never reached its own kill.
    if operation.process is not None and operation.process.returncode is None:
        with contextlib.suppress(ProcessLookupError):
            operation.process.kill()
        await operation.process.wait()


async def _drop(account_id: str) -> SignIn | None:
    """Stop this account's sign-in, held or finished, and forget its record."""
    operation = _SIGNINS.pop(account_id, None)
    if operation is not None:
        if operation.status == "waiting":
            operation.status = "cancelled"
        await _stop(operation)
    return operation


async def cancel_signin(account_id: str) -> dict[str, Any]:
    """Stop one held sign-in; the account keeps its state, unless it never signed in and nothing refers to it.

    Such a leftover is removed and its own home signed out (``account_leftovers``).

    A sign-in the vendor already finished is not cancelled: its real result is
    returned and its record stays.
    """
    operation = _SIGNINS.get(account_id)
    if operation is None:
        return {"account_id": account_id, "route": None, "status": "cancelled", "cause": None}
    exited = operation.process.returncode if operation.process is not None else None
    if operation.held and operation.task is not None and exited == 0:
        # The login already succeeded: let the status check and the commit finish.
        await asyncio.gather(asyncio.shield(operation.task), return_exceptions=True)
    if operation.status != "waiting":
        return operation.public()
    await _drop(account_id)
    if not operation.stop_failed:
        await remove_if_leftover(account_id)
    return operation.public()


async def stop_all_signins() -> None:
    """Stop held sign-ins at shutdown; accounts keep their state and vendor logout is never run."""
    operations = list(_SIGNINS.values())
    for operation in operations:
        if operation.status == "waiting":
            operation.status = "cancelled"
    # One login that will not stop must not leave the others running; only its record stays.
    results = await asyncio.gather(*(_stop(operation) for operation in operations), return_exceptions=True)
    for operation, result in zip(operations, results):
        account_id = operation.account_id
        if not isinstance(result, BaseException) and _SIGNINS.get(account_id) is operation:
            del _SIGNINS[account_id]
    for result in results:
        if isinstance(result, BaseException):
            raise result


async def verify(account_id: str, *, report_refresh: bool = False) -> dict[str, Any]:
    """Re-read vendor sign-in; only a definite signed-out answer flips a signed-in account.

    Verify never lifts a signed-out account or its hold; only sign-in does.
    """
    account = await custom_account(account_id)
    provider = account["provider"]
    receipt = account_receipt(account)
    current_c6 = readiness_reason(account) in ("codex_auth_projection_changed", "codex_auth_source_missing")
    binary = await _binary(provider)
    signed_in, _, cause = ((None, {}, profile_for(provider).missing_cause) if binary is None
                           else await _vendor_status(provider, account["source_home"], binary))
    if provider in VENDORS:
        await refresh_default_identity(provider)
    if account["state"] == "signed_in" and signed_in is False:
        account = await run_in_db(change_state, account_id, "signed_out", expected_revision=account["revision"])
    refresh_ok, refresh_cause, refresh_ref = None, None, None
    if account["state"] == "signed_in" and signed_in is True:
        refresh_ok, refresh_cause, refresh_ref = await refresh_models(account, receipt, binary, current_c6)
    result = {"account_id": account_id, "vendor_signed_in": signed_in, "cause": cause, "state": account["state"]}
    if report_refresh:
        result.update(model_refresh_ok=refresh_ok, model_refresh_cause=refresh_cause, model_refresh_ref=refresh_ref)
    return result


async def _leave(account_id: str, state: str, expected_revision: int) -> tuple[dict, dict]:
    """Commit the state and its holds first, then stop any sign-in and run the provider's sign-out.

    A failed sign-out leaves the committed state as it is and records a named
    cause: credentials may remain in the account's home. No directory is deleted.
    """
    account = await run_in_db(change_state, account_id, state, expected_revision=expected_revision)
    await _drop(account_id)
    return account, await account_cleanup.cleanup(account)


async def sign_out(account_id: str, *, expected_revision: int) -> tuple[dict, dict]:
    """Sign one account out; return it and its sign-out cleanup result."""
    return await _leave(account_id, "signed_out", expected_revision)


async def remove(account_id: str, *, expected_revision: int) -> tuple[dict, dict]:
    """Remove one account, keeping its tombstone and its private homes; return it and its cleanup result."""
    return await _leave(account_id, "removed", expected_revision)
