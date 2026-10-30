"""Baseline probes for account-scoped model catalog reconciliation."""
import pytest

from vendor_account_fixtures import accounts  # noqa: F401
from test_added_account_catalog import _discover, _row


def test_second_account_only_selector_is_stored_and_survives_other_refresh(accounts):
    db, default, first, second = accounts
    db.execute("UPDATE vendor_accounts SET state='removed', is_default=0 WHERE id=?", (default['id'],))
    db.commit()
    assert _discover(db, 'codex', second, [_row('second-only')]).status == 'ready'
    before = db.execute("SELECT available FROM models WHERE id='codex/second-only'").fetchone()
    assert before is not None and before['available'] == 1
    assert _discover(db, 'codex', first, [_row('first-only')]).status == 'ready'
    assert db.execute("SELECT available FROM models WHERE id='codex/second-only'").fetchone()['available'] == 1


@pytest.mark.parametrize('small_last', [True, False])
def test_shared_selector_uses_smallest_window_regardless_of_refresh_order(accounts, small_last):
    db, default, first, second = accounts
    db.execute("UPDATE vendor_accounts SET state='removed', is_default=0 WHERE id=?", (default['id'],))
    db.commit()
    small = _row('shared') | {'context_window': 100000}
    large = _row('shared') | {'context_window': 200000}
    order = [(first, small), (second, large)]
    if small_last:
        order.reverse()
    for account, row in order:
        assert _discover(db, 'codex', account, [row]).status == 'ready'
    assert db.execute("SELECT context_window FROM models WHERE id='codex/shared'").fetchone()[0] == 100000
    assert _discover(db, 'codex', first, [_row('other')]).status == 'ready'
    assert db.execute("SELECT context_window FROM models WHERE id='codex/shared'").fetchone()[0] == 200000


def test_other_account_refresh_preserves_existing_available_selector(accounts):
    from connections.harness_models import store_success

    db, default, first, second = accounts
    db.execute("UPDATE vendor_accounts SET state='removed', is_default=0 WHERE id=?", (default['id'],))
    store_success('codex', [_row('second-only')], harness_version='1', source='test', timestamp='t', db=db)
    db.commit()
    assert _discover(db, 'codex', second, [_row('second-only')]).status == 'ready'
    assert _discover(db, 'codex', first, [_row('first-only')]).status == 'ready'
    assert db.execute("SELECT available FROM models WHERE id='codex/second-only'").fetchone()['available'] == 1


def test_account_refresh_preserves_hide_and_saved_window(accounts):
    from connections.harness_models import store_success
    from llm.context_window import get_context_window_or_none
    from llm.model_status import scoped_status_rows

    db, default, account, _ = accounts
    db.execute("UPDATE vendor_accounts SET state='removed', is_default=0 WHERE id=?", (default['id'],))
    store_success('codex', [_row('hidden')], harness_version='1', source='test', timestamp='t', db=db)
    db.execute("UPDATE models SET enabled=0 WHERE id='codex/hidden'")
    db.execute("INSERT INTO model_profiles(model_id,context_window) VALUES ('codex/hidden',120000)")
    db.commit()
    assert _discover(db, 'codex', account, [_row('hidden')]).status == 'ready'
    assert db.execute("SELECT enabled FROM models WHERE id='codex/hidden'").fetchone()['enabled'] == 0
    assert get_context_window_or_none('codex/hidden', db=db) == 120000
    assert not any(row['id'] == 'codex/hidden' for row in scoped_status_rows(account_id=account['id']))
