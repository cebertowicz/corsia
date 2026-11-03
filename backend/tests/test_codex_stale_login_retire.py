"""A stored Codex app-server admitted on a login that is no longer current is retired before reuse.

Its frozen receipt can never pass the paid-write check again, so reusing it would
refuse every later Turn while the account itself is signed in. The next Turn starts
a fresh app-server on the account's current login instead.
"""
from __future__ import annotations

from types import SimpleNamespace

import pytest

from connections.account_receipts import capture_receipt
from connections.vendor_accounts import get_account, set_account_state
from subagents import codex_runtime_spawn, codex_runtime_support
from subagents.codex_runtime_state import _APP_SESSIONS, _LIVE
from vendor_account_fixtures import signed_in_default

_THREAD = "codex-stale-login-thread"


class _Session:
    def __init__(self, receipt):
        self.vendor_account_bound = True
        self.vendor_account_receipt = receipt
        self.returncode = None
        self.terminated = False

    async def terminate(self):
        self.terminated = True


@pytest.fixture
def signed_in(isolated_db):
    from infra.database import get_db

    db = get_db()
    account = signed_in_default("codex", db=db)
    yield db, account, capture_receipt("codex", account_id=account["id"], db=db)
    _APP_SESSIONS.pop(_THREAD, None)
    _LIVE.pop(_THREAD, None)
    db.close()


def _sign_in_again(db, account):
    current = get_account(account["id"], db=db)
    set_account_state(current["id"], "signed_in", expected_revision=current["revision"], db=db)
    db.commit()


@pytest.mark.asyncio
async def test_stored_session_on_a_replaced_login_is_retired(signed_in):
    db, account, receipt = signed_in
    session = _APP_SESSIONS[_THREAD] = _Session(receipt)
    _sign_in_again(db, account)

    assert await codex_runtime_spawn.reusable_app_session(_THREAD) is None
    assert session.terminated
    assert _THREAD not in _APP_SESSIONS


@pytest.mark.asyncio
async def test_stored_session_on_the_current_login_is_kept(signed_in):
    _db, _account, receipt = signed_in
    session = _APP_SESSIONS[_THREAD] = _Session(receipt)

    assert await codex_runtime_spawn.reusable_app_session(_THREAD) is session
    assert not session.terminated


@pytest.mark.asyncio
async def test_stale_session_a_live_turn_still_owns_is_left_to_that_turn(signed_in):
    db, account, receipt = signed_in
    session = _APP_SESSIONS[_THREAD] = _Session(receipt)
    _LIVE.put(_THREAD, SimpleNamespace(session=session))
    _sign_in_again(db, account)

    await codex_runtime_support.retire_stale_login_session(_THREAD)

    assert not session.terminated
    assert _APP_SESSIONS[_THREAD] is session
