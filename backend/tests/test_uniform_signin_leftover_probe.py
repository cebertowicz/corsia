"""Check whether a fresh duplicate is eligible for the leftover owner after cleanup."""
from vendor_account_fixtures import accounts  # noqa: F401


def test_fresh_duplicate_after_successful_cleanup_is_leftover(accounts):
    from connections.account_leftovers import commit_if_leftover
    from connections.vendor_accounts import add_account, record_auth_cleanup

    db = accounts[0]
    fresh = add_account('codex', 'Fresh', db=db)
    record_auth_cleanup(fresh['id'], {'status': 'done', 'cause': None, 'at': 't'}, db=db)
    removed = commit_if_leftover(fresh['id'], db=db)
    assert removed is not None and removed['state'] == 'removed'
