"""A Claude process exit follows the login captured by its Turn."""
import json
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from test_account_login_errors import EXPIRED, turn_on_other
from vendor_account_fixtures import accounts, claude  # noqa: F401


@pytest.mark.asyncio
async def test_process_exit_signs_out_added_login_before_its_one_completion(claude, monkeypatch):
    from connections import claude_discovery
    from connections.models import get_connection
    from connections.vendor_accounts import get_account
    from subagents import cc_process_exit

    db = claude.db
    turn_on_other(claude)
    before = get_connection('claude-code', db=db)
    default_reconcile = AsyncMock()
    monkeypatch.setattr(claude_discovery, 'reconcile_turn_auth_failure', default_reconcile)
    monkeypatch.setattr(cc_process_exit, 'consume_and_attach_terminal_context_usage', AsyncMock())
    identity = SimpleNamespace(turn_id='claude-turn', frame_fields=lambda: {'turn_id': 'claude-turn'})
    live = SimpleNamespace(tool_boundary_marker_id=None, last_assistant_message_id=None)
    frames = []

    def broadcast(_, payload):
        if payload.get('type') == 'turn_complete':
            assert get_account(claude.other, db=db)['state'] == 'signed_out'
        frames.append(payload)

    await cc_process_exit._publish_process_exit('claude-destination', live, identity, 1, EXPIRED, broadcast, db)
    row = db.execute("SELECT content,metadata FROM messages WHERE thread_id='claude-destination' AND id!='claude-input'").fetchone()
    assert row['content'] == 'Other is signed out of Claude Code. Sign it in again under Settings › Providers.'
    assert json.loads(row['metadata'])['account_id'] == claude.other
    assert json.loads(row['metadata'])['cause'] == 'process_exited'
    default_reconcile.assert_not_called()
    assert get_connection('claude-code', db=db) == before
    assert sum(frame.get('type') == 'turn_complete' for frame in frames) == 1
