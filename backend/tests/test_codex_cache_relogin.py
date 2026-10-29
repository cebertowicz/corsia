"""A stale account model cache has its own actionable admission error."""
import json
import logging
from types import SimpleNamespace

import pytest

from vendor_account_fixtures import accounts  # noqa: F401


@pytest.mark.asyncio
async def test_cache_failure_uses_turn_bound_account_and_own_cause(accounts):
    from infra.queries import insert_message
    from subagents.codex_runtime_errors import CodexModelCacheChanged, cache_error_marker

    db, _, account, _ = accounts
    db.execute("INSERT INTO active_turns(id,thread_id,execution_epoch,model,status,started_at,boot_epoch,vendor_account_id) "
               "VALUES ('codex-turn','destination',0,'codex/test','running','t','test',?)", (account['id'],))
    db.commit()
    marker = cache_error_marker(CodexModelCacheChanged(), 'destination', SimpleNamespace(turn_id='codex-turn'),
                                lambda *_: None, db, insert_message, logging.getLogger(__name__))
    mid = await marker()
    row = db.execute('SELECT content,metadata FROM messages WHERE id=?', (mid,)).fetchone()
    metadata = json.loads(row['metadata'])
    assert row['content'] == ("Couldn’t start Codex on Second: its model list is out of date. "
                              "Use Sign in again on Second under Settings › Providers.")
    assert 'ref' not in metadata
    assert metadata['reference']
    assert metadata['account_id'] == account['id']
    assert metadata['cause'] == 'account_model_cache_changed'
