"""Disposable probe of added-account catalog without an ambient Default."""
import pytest

from vendor_account_fixtures import accounts  # noqa: F401
from test_added_account_catalog import _discover, _row


@pytest.mark.parametrize('stored', [False, True])
@pytest.mark.parametrize('saved', [False, True])
def test_added_account_without_default_has_window_and_admission(accounts, stored, saved):
    from connections.account_selection import bind_model_account
    from infra.send_target import model_admission
    from llm.context_window import get_context_window_or_none
    from llm.model_status import scoped_status_rows

    db, default, account, other = accounts
    model = 'codex/kill-probe'
    db.execute("UPDATE vendor_accounts SET state='removed', is_default=0 WHERE id IN (?,?)", (default['id'], other['id']))
    db.execute("UPDATE connections SET status='needs_auth' WHERE provider='codex'")
    if stored:
        db.execute("INSERT INTO models(id,display_name,litellm_model,provider,provider_model_id,harness,available,context_window,created_at,updated_at) VALUES (?,?,?,?,?,'codex',1,200000,'t','t')", (model, 'Probe', model, 'codex', 'kill-probe'))
    if saved:
        db.execute("INSERT INTO model_profiles(model_id,context_window) VALUES (?,?)", (model, 120000))
    db.commit()
    assert _discover(db, 'codex', account, [_row('kill-probe')]).status == 'ready'
    row = next(item for item in scoped_status_rows(account_id=account['id']) if item['id'] == model)
    assert row['capabilities']['context_window_max'] == (120000 if saved else 200000)
    assert get_context_window_or_none(model, db=db) == (120000 if saved else 200000)
    assert bind_model_account('destination', model, account_id=account['id'], db=db) == account['id']
    assert model_admission(model, db=db, thread_id='destination') == 'run'
