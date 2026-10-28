"""The failed Codex launch publishes its account and readable recovery text."""
import json
import logging
from types import SimpleNamespace

import pytest

from infra.queries import insert_message
from vendor_account_fixtures import accounts  # noqa: F401


@pytest.mark.asyncio
async def test_cache_error_marker_stores_plain_text_and_account(accounts):
    from subagents.codex_runtime_errors import cache_error_marker, explain_model_cache_change

    db, _, account, _ = accounts
    db.execute("INSERT INTO active_turns(id,thread_id,execution_epoch,model,status,started_at,boot_epoch,vendor_account_id) "
               "VALUES ('turn','destination',0,'codex/test','running','t','test',?)", (account['id'],))
    db.commit()
    identity = SimpleNamespace(turn_id='turn')
    error = explain_model_cache_change(identity, logging.getLogger(__name__), db=db)
    marker = cache_error_marker(error, 'destination', SimpleNamespace(corsia_turn=identity),
                                lambda *_: None, db, insert_message, logging.getLogger(__name__))
    await marker()
    row = db.execute("SELECT content,metadata FROM messages WHERE thread_id='destination'").fetchone()
    assert row['content'] == str(error)
    assert 'account_model_cache_changed' not in row['content']
    assert json.loads(row['metadata'])['account_id'] == account['id']
