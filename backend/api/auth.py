"""Sign-in — the routes that turn a password into a token.

An account is created by ``corsia user add`` or by the pre-auth first-account
route ``/api/setup/first-account`` (``api/setup.py``): that route exists only
while no account exists, only for a direct loopback browser request, behind the
host, origin, forwarded-header, content-type and single-flight gates, and only
for the first account. Apart from it, the routes here are the only ones that
turn a password into a token.

Three endpoints, and each is public for a reason: ``status`` because the login
screen must be able to say "no account exists yet" before anyone can
authenticate, ``login`` because it *is* the way in, and ``logout`` because the
token it revokes is the only credential it needs.

Two properties are load-bearing, and both come from the backend binding
``0.0.0.0``: a password check is deliberately expensive (~128 MB and ~0.3 s of
scrypt), so anyone who can reach the port could otherwise exhaust memory by
starting many at once, or grind guesses in parallel.

**Hashing is serialized.** One asyncio lock, and the hash itself runs off the
event loop through the DB worker pool, so concurrent attempts queue instead of
multiplying: the cost of N attempts is N times one attempt, not N in parallel.

**Repeated failures for one username get slower.** A short grace, then a
doubling delay answered with 429 and ``Retry-After`` *before* any hashing, so a
refused attempt is cheap for the server and expensive for the caller. The
counter lives in this process: a restart clears it, which is honest rather than
load-bearing — scrypt's cost and the lock above are what bound throughput, and
this only removes the reward for hammering one name.

Neither a password nor a token value is ever logged or put in a URL here.
"""

from __future__ import annotations

import asyncio
import logging
import time

from fastapi import APIRouter, Header, HTTPException
from pydantic import BaseModel, Field

from infra import account_validation, accounts, token_admin
from infra.auth import authenticate, token_from_request
from infra.db_async import run_in_db
from infra.time import utc_storage_iso_to_api

router = APIRouter(prefix="/api/auth", tags=["auth"])
log = logging.getLogger(__name__)

#: One failed password check at a time, process-wide.
_login_lock = asyncio.Lock()

#: Failed attempts per username: ``{username: (count, last_failure_monotonic, is_account)}``.
_failures: dict[str, tuple[int, float, bool]] = {}

#: Failures allowed before a delay starts. Covers an ordinary typo or two.
_FREE_ATTEMPTS = 3
#: The delay after the first penalized failure; it doubles from there.
_BASE_DELAY_SECONDS = 5
#: Ceiling on that doubling, so a locked-out operator is never told to wait
#: an absurd time — five minutes already makes guessing hopeless.
_MAX_DELAY_SECONDS = 300
#: Idle time after which a username's failures are forgotten entirely.
_FAILURE_TTL_SECONDS = 3600
#: Most usernames tracked at once. Past this, expired records are swept and
#: then the stalest made-up name dropped, so made-up names cannot grow memory
#: unbounded or push a real account's count out.
_MAX_TRACKED_USERNAMES = 4096
#: Most attempts for made-up names allowed to wait for the hashing lock at
#: once. Accounts are not counted against it, so a burst of made-up names
#: cannot keep the owner from signing in.
_MAX_WAITING_ATTEMPTS = 16
#: How long an attempt for a made-up name is held when that cap is full,
#: before it is refused like a failed check.
_HELD_SECONDS = 1.0

_waiting_attempts = 0
#: Usernames with an attempt waiting for the lock: one at a time per name,
#: for every name alike, which also bounds the accounts' waiters.
_waiting_names: set[str] = set()

#: The one 401 for "no such account" and "wrong password". Telling them apart
#: would turn this route into a username oracle.
_INVALID = "Invalid username or password"


class LoginRequest(BaseModel):
    """Credentials posted by the sign-in form."""

    username: str = Field(min_length=1, max_length=64)
    password: str = Field(min_length=1, max_length=1024)


def _penalty_seconds(failures: int) -> int:
    """Return the delay owed after ``failures`` consecutive failures."""
    if failures <= _FREE_ATTEMPTS:
        return 0
    doubled = _BASE_DELAY_SECONDS * 2 ** (failures - _FREE_ATTEMPTS - 1)
    return min(doubled, _MAX_DELAY_SECONDS)


def _retry_after(username: str) -> int:
    """Seconds this username must still wait, or 0 when it may try now."""
    record = _failures.get(username)
    if record is None:
        return 0
    failures, last_failure, _ = record
    waited = time.monotonic() - last_failure
    if waited >= _FAILURE_TTL_SECONDS:
        _failures.pop(username, None)
        return 0
    remaining = _penalty_seconds(failures) - waited
    return max(0, int(remaining) + 1) if remaining > 0 else 0


def _record_failure(username: str, *, is_account: bool) -> None:
    """Count one failed attempt for ``username``, keeping the map bounded."""
    failures, _, _ = _failures.pop(username, (0, 0.0, is_account))
    now = time.monotonic()
    if len(_failures) >= _MAX_TRACKED_USERNAMES:
        for name in [n for n, (_, at, _) in _failures.items() if now - at >= _FAILURE_TTL_SECONDS]:
            del _failures[name]
    # Re-insertion below keeps the dict ordered stalest-first; accounts are never dropped.
    made_up = (n for n, (_, _, account) in _failures.items() if not account)
    while len(_failures) >= _MAX_TRACKED_USERNAMES and (stalest := next(made_up, None)) is not None:
        del _failures[stalest]
        made_up = (n for n, (_, _, account) in _failures.items() if not account)
    _failures[username] = (failures + 1, now, is_account)


def _refuse_if_penalized(username: str) -> None:
    """Answer 429 when ``username`` still owes a delay."""
    wait = _retry_after(username)
    if wait:
        raise HTTPException(
            429,
            f"Too many attempts. Try again in {wait} seconds.",
            headers={"Retry-After": str(wait)},
        )


def _clear_failures(username: str) -> None:
    """Forget ``username``'s failures after a successful sign-in."""
    _failures.pop(username, None)


@router.get("/status")
async def auth_status() -> dict:
    """Report whether this install has any account yet. Public by necessity."""
    has_users = await run_in_db(accounts.has_users)
    return {"has_users": has_users}


@router.post("/login")
async def login(body: LoginRequest) -> dict:
    """Exchange a username and password for a 30-day session token."""
    global _waiting_attempts
    try:
        username = account_validation.normalize_username(body.username)
    except account_validation.AccountError:
        # No account can carry a malformed name, so there is nothing to hash
        # and nothing to count; the answer is the same generic refusal.
        raise HTTPException(401, _INVALID) from None
    _refuse_if_penalized(username)
    if username in _waiting_names:
        raise HTTPException(
            429,
            "Too many sign-in attempts. Try again shortly.",
            headers={"Retry-After": "1"},
        )
    # Reserved before the first await, so two requests for one name can never both pass the check above.
    _waiting_names.add(username)
    counted = False
    try:
        is_account = await run_in_db(accounts.get_user, username) is not None
        if not is_account and _waiting_attempts >= _MAX_WAITING_ATTEMPTS:
            # Held and refused like a failed check, so the answer does not say the name is made up.
            await asyncio.sleep(_HELD_SECONDS)
            _record_failure(username, is_account=False)
            log.info("Sign-in refused")
            raise HTTPException(401, _INVALID)
        if not is_account:
            _waiting_attempts += 1
            counted = True
    except BaseException:
        _waiting_names.discard(username)
        raise
    try:
        async with _login_lock:
            # Re-checked under the lock: attempts that queued together must
            # not all skip a penalty the first of them earned.
            _refuse_if_penalized(username)
            user = await run_in_db(accounts.check_password, username, body.password)
            if not user:
                _record_failure(username, is_account=is_account)
    finally:
        _waiting_names.discard(username)
        if counted:
            _waiting_attempts -= 1
    if not user:
        log.info("Sign-in refused")
        raise HTTPException(401, _INVALID)

    _clear_failures(username)
    token, expires_at = await run_in_db(
        accounts.issue_token, user["id"], kind="session", label="browser"
    )
    log.info("Sign-in succeeded for %s", user["id"])
    return {
        "token": token,
        "user": user,
        "expires_at": utc_storage_iso_to_api(expires_at),
    }


@router.post("/logout")
async def logout(authorization: str | None = Header(None)) -> dict:
    """Revoke the exact token presented in the ``Authorization`` header."""
    token = token_from_request(None, authorization)
    if not authenticate(token):
        raise HTTPException(401, "Invalid or expired session")
    await run_in_db(token_admin.revoke_token, token)
    return {"ok": True}
