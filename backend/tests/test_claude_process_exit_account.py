"""A Claude process exit follows the login captured by its Turn."""
import json
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from test_account_login_errors import EXPIRED, turn_on_other
from vendor_account_fixtures import accounts, claude  # noqa: F401


@pytest.mark.asyncio
async def test_process_exit_signs_out_added_login_before_its_one_completion(claude, monkeypatch):
    from connections.models import get_connection
    from connections.vendor_accounts import get_account
    from subagents import cc_process_exit

    db = claude.db
    turn_on_other(claude)
    before = get_connection('claude-code', db=db)
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
    assert get_connection('claude-code', db=db) == before
    assert sum(frame.get('type') == 'turn_complete' for frame in frames) == 1


def test_signed_out_converted_default_is_named_by_its_label_not_default(claude):
    """A converted Default has no custom name: the line says who it was, as the account rows do."""
    from subagents.cc_runtime_errors import surface_exit_error
    from connections.vendor_accounts import get_account

    db = claude.db
    db.execute("UPDATE vendor_accounts SET state='signed_out', identity_json=? WHERE id=?",
               (json.dumps({'upgrade_reason': 'sign_in_to_keep_using_account',
                            'hint': {'email': 'old@example.test'}}), claude.first))
    db.commit()
    surface_exit_error('claude-destination', 1, EXPIRED, lambda *_: None, db, SimpleNamespace(turn_id='claude-turn'),
                       insert_message_fn=__import__('infra.queries', fromlist=['insert_message']).insert_message,
                       logger=__import__('logging').getLogger(__name__),
                       added_account=get_account(claude.first, db=db))
    row = db.execute("SELECT content FROM messages WHERE thread_id='claude-destination' AND id!='claude-input'").fetchone()
    assert row['content'] == 'old@example.test is signed out of Claude Code. Sign it in again under Settings › Providers.'
