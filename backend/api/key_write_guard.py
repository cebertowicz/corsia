"""The account password on key writes, and what three wrong ones do.

Set, Replace, Clear and Remove in Settings › Keys need a signed-in person's
session *and* the account password in the same request, because an agent
driving a browser that holds the person's session would otherwise pass the
session check alone. The password is checked with sign-in's own check
(``accounts.check_password``) for the session's user; a username in the
request is never read.

Load-bearing properties:

- **Wrong passwords here never touch sign-in's throttle** (``api.auth``), so
  an agent cannot use them to lock the owner out of sign-in.
- **Three wrong in a row end that session only** (``accounts.revoke_token``);
  the person's other sessions stay signed in. The count is per session in the
  database, so a restart does not reset it; only a correct password does.
- **An attempt is reserved before it waits.** Each request takes a place in
  the session's count before it waits on this module's lock, so a fourth
  parallel request is refused before its password is hashed and nothing
  queues without limit. Checks then run one at a time under a lock built like
  sign-in's but separate from it, so parallel guesses cannot get past three
  and a key-write check never holds up sign-in.

The password, like a key value, never appears in a response, log, audit
record, exception text, trace or telemetry: nothing here formats it, and the
routes parse their own bodies so a validation error cannot echo it.
"""

from __future__ import annotations

import asyncio
import sqlite3
from typing import Callable

from fastapi import HTTPException

from infra import accounts
from infra.database import write_transaction
from infra.db_async import run_in_db
from infra.time import utc_now_iso

#: Wrong passwords in a row that end the session.
MAX_WRONG = 3
#: Bounded as sign-in's password field is (``api.auth.LoginRequest``).
PASSWORD_MAX = 1024

MISMATCH = "That password didn't match. Nothing was changed."
SIGNED_OUT = "You were signed out after three wrong passwords. Nothing was changed."
BUSY = "Another change is still being checked. Try again in a moment."
NO_PASSWORD = "Enter your CORSIA password."
NOT_SAVED = "Couldn't save that. Nothing was changed."

#: One key-write password check at a time, process-wide; never sign-in's lock.
_key_write_lock = asyncio.Lock()

#: Reserved, not yet settled checks per session digest.
_pending: dict[str, int] = {}


def refusal(status: int, code: str, message: str) -> HTTPException:
    """A refusal whose body carries a fixed code and message, never input."""
    return HTTPException(status, {"code": code, "message": message})


def _failures(digest: str, db: sqlite3.Connection | None = None) -> int:
    from infra.database import get_db

    row = (db or get_db()).execute(
        "SELECT failures FROM key_write_attempts WHERE token_hash = ?", (digest,)
    ).fetchone()
    return int(row["failures"]) if row else 0


def _session_alive(token: str) -> bool:
    resolved = accounts.resolve_token(token)
    return bool(resolved) and resolved.get("token_kind") == "session"


def _record_wrong(token: str) -> bool:
    """Count one wrong password; end the session at three. True iff it ended."""
    digest = accounts.token_digest(token)
    with write_transaction(None, scope="key_write_wrong") as conn:
        row = conn.execute(
            "INSERT INTO key_write_attempts (token_hash, failures, updated_at)"
            " VALUES (?, 1, ?) ON CONFLICT(token_hash) DO UPDATE SET"
            " failures = failures + 1, updated_at = excluded.updated_at"
            " RETURNING failures",
            (digest, utc_now_iso()),
        ).fetchone()
        if int(row[0]) < MAX_WRONG:
            return False
        conn.execute("DELETE FROM key_write_attempts WHERE token_hash = ?", (digest,))
        accounts.revoke_token(token, db=conn)
        return True


def _reset(token: str) -> None:
    with write_transaction(None, scope="key_write_reset") as conn:
        conn.execute(
            "DELETE FROM key_write_attempts WHERE token_hash = ?",
            (accounts.token_digest(token),),
        )


def _checked_password(password: object) -> str:
    if not isinstance(password, str) or not password:
        raise refusal(400, "password_required", NO_PASSWORD)
    if len(password) > PASSWORD_MAX:
        raise refusal(400, "invalid_request", NOT_SAVED)
    return password


async def verified_write(token: str, user_id: str, password: object, effect: Callable[[], None]) -> None:
    """Run ``effect`` iff ``password`` is the session user's, counting wrong ones.

    ``effect`` runs on the DB pool under the key-write lock, after the count
    resets. A refusal raised here never names the password or a value.
    """
    password = _checked_password(password)
    digest = accounts.token_digest(token)
    failures = await run_in_db(_failures, digest)
    # No await between this check and the reservation, so parallel requests
    # each see the places taken before them.
    if failures + _pending.get(digest, 0) >= MAX_WRONG:
        raise refusal(429, "key_write_busy", BUSY)
    _pending[digest] = _pending.get(digest, 0) + 1
    try:
        async with _key_write_lock:
            if not await run_in_db(_session_alive, token):
                raise refusal(401, "signed_out", SIGNED_OUT)
            user = await run_in_db(accounts.check_password, user_id, password)
            if not user or user.get("id") != user_id:
                if await run_in_db(_record_wrong, token):
                    raise refusal(401, "signed_out", SIGNED_OUT)
                raise refusal(403, "password_mismatch", MISMATCH)
            await run_in_db(_reset, token)
            await run_in_db(effect)
    finally:
        remaining = _pending.get(digest, 1) - 1
        if remaining > 0:
            _pending[digest] = remaining
        else:
            _pending.pop(digest, None)
