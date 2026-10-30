"""CI-only real Safehouse outside-PR pipeline; GitHub transport and model are fakes.

The stage script, corsia CLI, broker channel, intake, recipient delivery, worker
and settlement run through their actual implementations. Never run this nested
sandbox locally.
"""
import json
import subprocess

import pytest

from tests.test_github_screen_path import _route, screen  # noqa: F401
from tests.test_script_sandbox_real import sandbox, short_system_temp  # noqa: F401
from tests.rule_worker_helpers import run_scripts, settle_all, starts, worker_events, world  # noqa: F401

pytestmark = pytest.mark.integration


@pytest.mark.asyncio
async def test_real_safehouse_outside_pr_stage_through_worker(screen, sandbox, monkeypatch):
    from automation import script_github_channel, script_runner, script_sandbox
    from connections.github_broker import GithubReadBroker
    from events import delivery
    from infra import config
    from infra.database import get_db, write_transaction
    from infra.db_async import run_in_db
    from infra.send_service import send_thread_input
    from tools import sandbox_config
    from pathlib import Path

    calls = []

    def transport(args, *, timeout):
        calls.append(args)
        endpoint = args[-1]
        if '/compare/' in endpoint:
            data = 'diff --git a/a.txt b/a.txt\n--- a/a.txt\n+++ b/a.txt\n@@ -0,0 +1 @@\n+hello\n'
        elif '/git/commits/' in endpoint:
            data = json.dumps({'tree': {'sha': 'c' * 40}})
        elif '/git/trees/' in endpoint:
            data = json.dumps({'tree': []})
        else:
            data = json.dumps({'head': {'sha': screen['state']['sha']}, 'base': {'sha': 'b' * 40},
                               'title': 'TITLE_CANARY', 'body': 'BODY_CANARY'})
        return subprocess.CompletedProcess(args, 0, data, '')

    # The shared fixture records webhook sends; restore real delivery and use its
    # fake Claude executable for the distinct PR owner as well as the worker.
    monkeypatch.setattr(delivery, 'send_thread_input', send_thread_input)
    def select_fake_recipient():
        with write_transaction(None, scope='screen_test_recipient') as db:
            db.execute("UPDATE threads SET model='claude-code/sonnet' WHERE id='pr-owner'")

    await run_in_db(select_fake_recipient)
    stage_path = Path(get_db().execute(
        'SELECT script_path FROM automation_schedules WHERE id=?',
        (screen['key'] + '_script',)).fetchone()[0]).resolve()
    assert stage_path.is_relative_to(Path(config.WORKSPACE).resolve())
    monkeypatch.setattr(script_github_channel, 'default_broker', lambda: GithubReadBroker(transport))
    monkeypatch.setattr(script_runner, 'run_script', script_sandbox.run_script)
    monkeypatch.setattr(sandbox_config, '_STATIC_ALLOWED_RW', [sandbox.parent, Path(config.WORKSPACE).resolve()])
    await _route(screen['state']['sha'])
    await run_scripts()
    await settle_all()
    assert [row[0] for row in starts(screen['key'] + '_start')] == ['returned']
    assert [kind for kind, _ in worker_events()] == ['worker.result']
    assert len(calls) >= 6
    assert not list((screen['folder'] / 'staging').glob('screen-*'))
    result = next(a['facts'] for a in screen['state']['admitted'] if a['event_type'] == 'worker.result')
    assert result['item'] == 'o/r#1' and result['item_version'] == screen['state']['sha']
    assert result['source'] == 'outside' and result['valid'] is True
    assert json.loads(result['result'])['verdict'] == 'safe'
    messages = get_db().execute('SELECT content FROM messages WHERE thread_id=?', ('pr-owner',)).fetchall()
    assert any('[EVENT: result]' in row[0] for row in messages)
    assert all('CANARY' not in json.dumps(a.get('facts')) for a in screen['state']['admitted'])
