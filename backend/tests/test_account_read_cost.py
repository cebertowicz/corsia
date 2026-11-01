"""The account-aware catalog read costs the same statements for one model or many."""
import pytest

from vendor_account_fixtures import accounts, signed_in_default  # noqa: F401
from test_added_account_catalog import _discover, _row


def _statements(read) -> list[str]:
    from infra.database import get_db

    db = get_db()
    seen: list[str] = []
    # A model's own saved window is one read per model by design; every account read is per request.
    db.set_trace_callback(lambda sql: seen.append(sql) if 'FROM model_profiles' not in sql else None)
    try:
        read()
    finally:
        db.set_trace_callback(None)
    return seen


def _seed(db, count):
    from connections.vendor_accounts import list_accounts

    for account in list_accounts(provider='codex', db=db):
        if account['state'] == 'signed_in':
            assert _discover(db, 'codex', account, [_row(f'm{index}') for index in range(count)]).status == 'ready'


@pytest.mark.parametrize('scope', ['workspace', 'thread'])
def test_scoped_catalog_cost_does_not_grow_with_models(accounts, scope):
    from llm.model_status import scoped_status_rows

    db = accounts[0]
    kwargs = {'thread_id': 'actor'} if scope == 'thread' else {}
    costs = {}
    for count in (1, 6):
        _seed(db, count)
        scoped_status_rows(**kwargs)  # prime lazy imports and caches
        costs[count] = len(_statements(lambda: scoped_status_rows(**kwargs)))
    assert costs[1] == costs[6], costs
