"""A Claude process exit follows the account captured by its Turn."""
import json
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from vendor_account_fixtures import accounts, claude  # noqa: F401


@pytest.mark.asyncio
async def test_process_exit_uses_added_account_and_emits_one_completion(claude, monkeypatch):
    from connections import account_controls, claude_discovery
    from connections.models import get_connection
    from subagents import cc_process_exit, cc_runtime_errors

    db = claude.db
    db.execute("UPDATE active_turns SET vendor_account_id=? WHERE id='claude-turn'", (claude.other,))
    db.commit()
    before = get_connection('claude-code', db=db)
    default_reconcile = AsyncMock()
    account_verify = AsyncMock()
    monkeypatch.setattr(claude_discovery, 'reconcile_turn_auth_failure', default_reconcile)
    monkeypatch.setattr(account_controls, 'verify', account_verify)
    monkeypatch.setattr(cc_process_exit, 'consume_and_attach_terminal_context_usage', AsyncMock())
    identity = SimpleNamespace(turn_id='claude-turn', frame_fields=lambda: {'turn_id': 'claude-turn'})
    live = SimpleNamespace(tool_boundary_marker_id=None, last_assistant_message_id=None)
    frames = []
    await cc_process_exit._publish_process_exit(
        'claude-destination', live, identity, 1, 'Not logged in · Please run /login',
        lambda _, payload: frames.append(payload), db,
    )
    for task in tuple(cc_runtime_errors._EXPIRED_LOGIN_RECONCILIATION_TASKS):
        await task
    row = db.execute("SELECT content,metadata FROM messages WHERE thread_id='claude-destination'").fetchone()
    assert row['content'] == 'Other is signed out of Claude Code. Sign it in again under Settings › Providers.'
    assert json.loads(row['metadata'])['account_id'] == claude.other
    account_verify.assert_awaited_once_with(claude.other, refresh_default=False)
    default_reconcile.assert_not_awaited()
    assert get_connection('claude-code', db=db) == before
    assert sum(frame.get('type') == 'turn_complete' for frame in frames) == 1
