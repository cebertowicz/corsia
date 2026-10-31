"""Duplicate sign-in keeps referenced Default identities while recovering the target."""
from unittest.mock import AsyncMock

import pytest

from vendor_account_fixtures import accounts  # noqa: F401


@pytest.mark.asyncio
@pytest.mark.parametrize('case', ['default_signer', 'default_target', 'fresh_signer'])
async def test_unchecked_duplicate_preserves_default_and_removes_fresh(accounts, monkeypatch, case):
    from connections.account_controls import SignIn
    from connections.account_signin_retry import retry_unchecked
    from connections.vendor_accounts import add_account, get_account, record_auth_cleanup, set_account_state

    db, old_default, target, _ = accounts
    db.execute("UPDATE vendor_accounts SET state='removed', is_default=0 WHERE id=?", (old_default['id'],))
    db.commit()
    signer = add_account('codex', 'Signing in', db=db)
    if case == 'default_signer':
        db.execute('UPDATE vendor_accounts SET is_default=1 WHERE id=?', (signer['id'],))
    elif case == 'default_target':
        db.execute('UPDATE vendor_accounts SET is_default=1 WHERE id=?', (target['id'],))
    db.commit()
    operation = SignIn(signer['id'], 'codex', signer['revision'])
    finish = AsyncMock()
    monkeypatch.setattr('connections.account_signin_retry.finish_signin', finish)

    async def cleanup(account):
        record_auth_cleanup(account['id'], {'status': 'done', 'cause': None, 'at': 't'}, db=db)

    await retry_unchecked(operation, target['id'], '/synthetic/codex', cleanup)
    assert operation.status == 'failed' and operation.cause == 'same_account'
    assert get_account(signer['id'], db=db)['state'] == ('signed_out' if case == 'default_signer' else 'removed')
    assert get_account(target['id'], db=db)['state'] == 'signed_in'
    finish.assert_awaited_once()


@pytest.mark.asyncio
async def test_rename_during_cleanup_does_not_fail_the_duplicate_recovery(accounts, monkeypatch):
    from connections.account_controls import SignIn
    from connections.account_signin_retry import retry_unchecked
    from connections.vendor_accounts import add_account, get_account, rename_account

    db, old_default, target, _ = accounts
    db.execute("UPDATE vendor_accounts SET state='removed', is_default=0 WHERE id=?", (old_default['id'],))
    db.commit()
    signer = add_account('codex', 'Signing in', db=db)
    operation = SignIn(signer['id'], 'codex', signer['revision'])
    monkeypatch.setattr('connections.account_signin_retry.finish_signin', AsyncMock())

    async def cleanup(account):
        rename_account(account['id'], 'Renamed meanwhile', expected_revision=account['revision'], db=db)

    await retry_unchecked(operation, target['id'], '/synthetic/codex', cleanup)
    assert operation.status == 'failed' and operation.cause == 'same_account'
    assert get_account(signer['id'], db=db)['state'] == 'removed'
