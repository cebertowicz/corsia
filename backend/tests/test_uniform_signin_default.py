"""Default assignment on sign-in, including removed Default tombstones."""
import pytest

from vendor_account_fixtures import accounts  # noqa: F401


@pytest.mark.parametrize('case', ['no_default', 'removed_default', 'signed_out_default', 'converted_default'])
def test_signin_selects_default_only_when_missing(accounts, case):
    from connections.account_signin_commit import commit_signed_in
    from connections.vendor_accounts import add_account, get_account, resolve_account_id

    db, old_default, other, _ = accounts
    if case == 'no_default':
        db.execute("UPDATE vendor_accounts SET is_default=0, state='removed' WHERE id=?", (old_default['id'],))
    elif case == 'removed_default':
        db.execute("UPDATE vendor_accounts SET state='removed' WHERE id=?", (old_default['id'],))
        assert resolve_account_id('codex', db=db) is None
        assert get_account(other['id'], db=db)['state'] == 'signed_in'
        assert get_account(other['id'], db=db)['is_default'] == 0
    else:
        db.execute("UPDATE vendor_accounts SET state='signed_out' WHERE id=?", (old_default['id'],))
    db.commit()
    signer = add_account('codex', 'Fresh', db=db)
    if case == 'converted_default':
        db.execute('UPDATE vendor_accounts SET source_home=?,runtime_home=? WHERE id=?',
                   (signer['source_home'], signer['runtime_home'], old_default['id']))
        db.commit()
        signer = get_account(old_default['id'], db=db)
    commit_signed_in(signer['id'], signer['revision'], {})
    chosen = resolve_account_id('codex', db=db)
    assert chosen == (old_default['id'] if case in ('signed_out_default', 'converted_default') else signer['id'])
    if case == 'removed_default':
        assert get_account(old_default['id'], db=db)['is_default'] == 0
    if case in ('no_default', 'removed_default'):
        later = add_account('codex', 'Later', db=db)
        commit_signed_in(later['id'], later['revision'], {})
        assert resolve_account_id('codex', db=db) == signer['id']


def test_unbound_turn_uses_permitted_account_after_default_removed(accounts):
    from connections.account_evidence import mark_discovery
    from connections.account_policy import admit_account, turn_account
    from connections.account_receipts import capture_receipt
    from connections.vendor_accounts import resolve_account_id

    db, default, second, _ = accounts
    db.execute("UPDATE vendor_accounts SET state='removed' WHERE id=?", (default['id'],))
    receipt = capture_receipt('codex', account_id=second['id'], db=db)
    assert receipt is not None
    assert mark_discovery(second['id'], receipt=receipt, status='ready',
                          models={'codex/test': {}}, db=db)
    db.commit()
    assert resolve_account_id('codex', db=db) is None
    identity, reason = turn_account('codex', thread_id='destination', model='codex/test', db=db)
    assert (identity, reason) == (second['id'], None)
    assert admit_account('codex', identity, ['destination-project'], execution=True,
                         model='codex/test', db=db) == second['id']
