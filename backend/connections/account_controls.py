"""Vendor-owned sign-in, status and sign-out for an added account's own home.

CORSIA retains only a hash when comparing Codex logins; each vendor CLI writes and
removes its own for the home it is given. Sign-in is the vendor's browser
route, held in memory until the vendor process exits or the person cancels; a
restart drops it and the account stays signed out. Only the vendor's status
command decides whether an account is signed in. Default identity is read only
at completion and verify; its login is never changed here.
"""
from __future__ import annotations

import asyncio
import contextlib
import json
import logging
from typing import Any

from connections import account_signin_prompt, harness_models
from connections.account_identity import codex_identity, refresh_default_identity
from connections.account_lifecycle import change_state
from connections.account_profiles import profile_for, sign_out_account
from connections.account_receipts import account_receipt
from connections.account_signin import SignIn, failure_reference
from connections.account_evidence import readiness_reason
from connections.account_signin_commit import SameAccount, commit_signed_in
from connections.corsia_agent_account import FirstRunBind, finish_signin
from connections.harness_process import killed_on_exit
from connections.vendor_accounts import (
    AUTH_CLEANUP_CAUSES,
    VENDORS,
    AccountError,
    duplicate_of,
    get_account,
    record_auth_cleanup,
)
from infra.db_async import run_in_db
from infra.time import utc_now_iso

log = logging.getLogger(__name__)
# Bounds the short status and logout commands only; a held sign-in has no timeout.
_COMMAND_SECONDS = 30.0
_CLEANUP_STATUSES = ("done", "failed", "skipped", "pending")


_SIGNINS: dict[str, SignIn] = {}


async def _custom(account_id: str) -> dict:
    account = await run_in_db(get_account, account_id)
    if account["source_home"] is None:
        raise AccountError("the Default account signs in through the vendor's own CLI")
    if account["state"] == "removed":
        raise AccountError("account was removed; choose another permitted account")
    return account


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


async def _hold(operation: SignIn, home: str, binary: str) -> None:
    """Wait for the vendor login to exit by itself, then let the vendor's status decide."""
    process = operation.process
    assert process is not None and process.stdout is not None
    try:
        # Read and drop the output so the vendor never blocks on a full pipe; a
        # descendant holding the pipe open must not delay the exit either.
        drain = asyncio.ensure_future(account_signin_prompt.discard(process.stdout, operation))
        async with killed_on_exit(process):
            try:
                await process.wait()
            finally:
                # Settle the reader before anything else reads the pipe to reap the process.
                drain.cancel()
                await asyncio.gather(drain, return_exceptions=True)
        if process.returncode != 0:
            operation.status, operation.cause = "failed", "vendor_login_failed"
            return
        signed_in, identity, cause = await _vendor_status(operation.provider, home, binary)
        if signed_in is not True:
            operation.status, operation.cause = "failed", cause or "vendor_not_signed_in"
            return
        refusal = profile_for(operation.provider).signin_refusal
        if refusal is not None and (refused := await run_in_db(refusal, home, identity)) is not None:
            # The login stands in the home until the vendor's own logout removes it.
            await _cleanup(await run_in_db(get_account, operation.account_id))
            operation.status, operation.cause = "failed", refused
            return
        if operation.provider in VENDORS:
            await refresh_default_identity(operation.provider)
        try:
            operation.duplicate_of, receipt = await run_in_db(
                commit_signed_in, operation.account_id, operation.revision, identity)
        except SameAccount as repeat:
            operation.duplicate_of = str(repeat)
            await _cleanup(await run_in_db(get_account, operation.account_id))
            operation.status, operation.cause = "failed", "same_account"
            return
        except AccountError:
            operation.status, operation.cause = "superseded", "account_changed"
            return
        operation.status, operation.cause = "completed", None
        await finish_signin(operation, receipt, binary)
    except Exception as exc:  # noqa: BLE001 - nobody may be left waiting on a sign-in nothing is waiting on
        log.warning("account %s sign-in check failed: %s", operation.account_id, type(exc).__name__)
        if operation.status == "waiting":
            operation.status, operation.cause = "failed", "signin_check_failed"
    finally:
        if operation.status == "failed":
            operation.ref = failure_reference(operation.provider, operation.cause)


async def start_signin(account_id: str, *, bind: FirstRunBind | None = None) -> dict[str, Any]:
    """Begin, or return the live, vendor browser sign-in for one signed-out added account."""
    account = await _custom(account_id)
    if account["state"] != "signed_out":
        raise AccountError("account is already signed in")
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
            operation.ref = failure_reference(provider, operation.cause)
            return operation.public()
    if operation.status != "waiting":  # cancelled while the stale login was removed
        return operation.public()
    try:
        operation.process = await _spawn(provider, account["source_home"], binary, "login")
    except OSError:
        operation.status, operation.cause = "failed", "vendor_login_unavailable"
        operation.ref = failure_reference(provider, operation.cause)
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
    """Stop one held sign-in; the account stays signed out and the vendor's logout is never run.

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
    return operation.public()


async def stop_all_signins() -> None:
    """Stop every held sign-in when the backend shuts down; each account stays signed out."""
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


async def verify(account_id: str) -> dict[str, Any]:
    """Re-read vendor sign-in; only a definite signed-out answer flips a signed-in account.

    Verify never lifts a signed-out account or its hold; only sign-in does.
    """
    account = await _custom(account_id)
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
    if (provider in VENDORS and account["state"] == "signed_in" and signed_in is True
            and receipt is not None and not current_c6):
        try:
            await harness_models.refresh_provider(provider, reason="account-verify", account_id=account_id,
                expected_receipt=receipt, binary=binary)
        except Exception as exc:
            log.warning("account %s discovery failed: %s", account_id, type(exc).__name__)
    return {"account_id": account_id, "vendor_signed_in": signed_in, "cause": cause, "state": account["state"]}


async def vendor_logout(account: dict) -> dict[str, Any]:
    """Run the vendor's own logout for this account's stored home; nothing falls back to another home."""
    provider = account["provider"]
    binary = await _binary(provider)
    if binary is None:
        return {"status": "failed", "cause": profile_for(provider).missing_cause}
    try:
        code, _ = await _run(provider, account["source_home"], binary, "logout")
    except (OSError, TimeoutError):
        return {"status": "failed", "cause": "vendor_logout_unavailable"}
    return {"status": "done", "cause": None} if code == 0 else {"status": "failed", "cause": "vendor_logout_failed"}


async def _leave(account_id: str, state: str, expected_revision: int) -> tuple[dict, dict]:
    """Commit the state and its holds first, then stop any sign-in and run the provider's sign-out.

    A failed sign-out leaves the committed state as it is and records a named
    cause: credentials may remain in the account's home. No directory is deleted.
    """
    account = await run_in_db(change_state, account_id, state, expected_revision=expected_revision)
    await _drop(account_id)
    return account, await _cleanup(account)


async def _cleanup(account: dict) -> dict:
    """Run and record the same provider cleanup, also for a refused login."""
    account_id = account["id"]
    try:
        result = await sign_out_account(account)
        cleanup = {"status": result["status"], "cause": result.get("cause")}
        # Only a fixed name is stored or shown, never free text from a provider.
        named = isinstance(cleanup["cause"], (str, type(None))) and cleanup["cause"] in AUTH_CLEANUP_CAUSES
        if cleanup["status"] not in _CLEANUP_STATUSES or not named:
            cleanup = {"status": "failed", "cause": "sign_out_result_invalid"}
    except Exception as exc:  # noqa: BLE001 - a provider hook's failure must not undo the committed state
        log.warning("account %s sign-out hook failed: %s", account_id, type(exc).__name__)
        cleanup = {"status": "failed", "cause": "sign_out_hook_failed"}
    cleanup["at"] = utc_now_iso()
    await run_in_db(record_auth_cleanup, account_id, cleanup)
    return cleanup


async def sign_out(account_id: str, *, expected_revision: int) -> tuple[dict, dict]:
    """Sign one account out; return it and its sign-out cleanup result."""
    return await _leave(account_id, "signed_out", expected_revision)


async def remove(account_id: str, *, expected_revision: int) -> tuple[dict, dict]:
    """Remove one account, keeping its tombstone and its private homes; return it and its cleanup result."""
    return await _leave(account_id, "removed", expected_revision)
