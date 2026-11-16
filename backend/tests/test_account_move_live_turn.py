"""Delayed account-move publication cannot retire the Turn on its new login."""
from __future__ import annotations

import asyncio
import sys
from pathlib import Path

import pytest

from cc_runtime_fixtures import _FAKE, _wait, cc_target


def _gated_cli(tmp_path):
    script = _FAKE.format(python=sys.executable)
    script = script.replace('import sys, json', 'import sys, json, os, time, select\nfrom pathlib import Path')
    script = script.replace(
        '    emit({"type":"result"',
        '    if "moved-input" in c:\n'
        '        Path(os.environ["MOVE_SEEN"]).touch()\n'
        '        while not Path(os.environ["MOVE_RELEASE"]).exists():\n'
        '            if select.select([sys.stdin], [], [], 0.01)[0]:\n'
        '                if not sys.stdin.readline():\n'
        '                    sys.exit(0)\n'
        '    emit({"type":"result"',
    )
    binary = tmp_path / 'gated-claude'
    binary.write_text(script)
    binary.chmod(0o755)
    return str(binary)


def _accounts(db, provider='claude-code'):
    from connections.account_evidence import mark_discovery
    from connections.account_policy import select_account
    from connections.account_receipts import capture_receipt
    from connections.vendor_accounts import add_account, set_account_state
    from vendor_account_fixtures import signed_in_default

    first = signed_in_default(provider, db=db)
    second = add_account(provider, 'Second', db=db)
    if provider == 'codex':
        Path(second['source_home'], 'auth.json').write_text(
            '{"auth_mode":"chatgpt","tokens":{"access_token":"synthetic-test-login"}}')
    second = set_account_state(second['id'], 'signed_in', expected_revision=0, db=db)
    receipt = capture_receipt(provider, account_id=second['id'], db=db)
    model = 'codex/gpt-5-codex' if provider == 'codex' else 'claude-code/sonnet'
    assert mark_discovery(second['id'], receipt=receipt, status='ready', models={
        model: {'available': True, 'context_window': 200000},
    }, db=db)
    if provider == 'codex':
        from connections.model_cache_stage import ModelCacheStage

        for account in (first, second):
            source = Path(account['runtime_home']).parent / (account['id'] + '-catalog.json')
            source.write_text('{"models":[{"slug":"gpt-5-codex"}]}')
            target = Path(account['runtime_home']) / 'models_cache.json'
            cache = ModelCacheStage.prepare(source, target).promote()
            login = capture_receipt(provider, account_id=account['id'], db=db)
            assert mark_discovery(account['id'], receipt=login, status='ready', models={
                model: {'available': True, 'context_window': 200000},
            }, cache_receipt=cache, db=db)
    select_account('move-thread', provider, account_id=first['id'], db=db)
    db.commit()
    return first, second


def _admit(db, content, number):
    from core.turn_core import BOOT_EPOCH
    from infra.thread_driver import admit_input

    return admit_input('move-thread', role='user', content=content, intent='apply_now',
                       message_id=f'input-{number}', turn_id=f'turn-{number}',
                       boot_epoch=BOOT_EPOCH, admitted_at='t', db=db)


async def _execute(db, batch, target=None):
    from infra.thread_execution import execute_claimed_batch

    row = dict(db.execute("SELECT * FROM threads WHERE id='move-thread'").fetchone())
    task = asyncio.create_task(execute_claimed_batch(batch, thread_row=row, target=target or cc_target('sonnet')))
    done, _ = await asyncio.wait({task}, timeout=10)
    if not done:
        task.cancel()
        raise AssertionError('claimed batch did not return')
    await task


@pytest.mark.asyncio
async def test_delayed_move_effect_does_not_strand_new_account_turn(
    isolated_db, signed_in_defaults, monkeypatch, tmp_path,
):
    from connections import account_moves
    from connections.account_moves import hold_pending_account
    from infra.database import get_db
    from subagents.cc_runtime_state import _LIVE

    binary = _gated_cli(tmp_path)
    monkeypatch.setenv('MOVE_SEEN', str(tmp_path / 'seen'))
    monkeypatch.setenv('MOVE_RELEASE', str(tmp_path / 'release'))
    monkeypatch.setattr('connections.models.get_connection', lambda *_a, **_k: {'status': 'available', 'config': {'binary': binary}})
    receipts = []
    monkeypatch.setattr(account_moves, '_publish', receipts.append)
    monkeypatch.setattr('infra.connection_limit_drive.schedule_limited_heads', lambda *_: None)
    monkeypatch.setattr('infra.unavailable_model_recovery.schedule_unavailable_model_recovery', lambda: None)
    db = get_db()
    db.execute("INSERT INTO threads(id,user_id,model,created_at,updated_at) "
               "VALUES ('move-thread','user-1','claude-code/sonnet','t','t')")
    db.commit()
    _, second = _accounts(db)
    effect = None
    try:
        warm = _admit(db, 'warmup', 0)
        await _execute(db, warm.batch)
        assert await _wait(lambda: db.execute("SELECT status FROM active_turns WHERE id='turn-0'").fetchone()[0] == 'completed')
        old = _LIVE.get('move-thread').session
        hold_pending_account('move-thread', 'claude-code', second['id'], db=db)
        db.commit()
        moved = _admit(db, 'moved-input', 1)
        assert moved.batch is not None and len(receipts) == 1
        execution = asyncio.create_task(_execute(db, moved.batch))
        assert await _wait(lambda: (tmp_path / 'seen').exists())
        current = _LIVE.get('move-thread').session
        assert current is not old and old.returncode is not None
        assert current.vendor_account_id == second['id']
        assert _admit(db, 'queued-input', 2).batch is None
        effect = asyncio.create_task(account_moves._post_effects(receipts[0]))
        done, _ = await asyncio.wait({effect}, timeout=5)
        assert current.returncode is None, (
            'account-move teardown killed the new-account CLI and stranded its successor: '
            + repr([dict(row) for row in db.execute('SELECT id,status FROM active_turns')]))
        assert done, 'after-commit retirement stranded the successor under its transition lock'
        await effect
        await execution
        (tmp_path / 'release').touch()
        assert await _wait(lambda: db.execute("SELECT status FROM active_turns WHERE id='turn-1'").fetchone()[0] == 'completed')
        assert await _wait(lambda: db.execute("SELECT delivery_state FROM messages WHERE id='input-2'").fetchone()[0] == 'delivered')
        assert await _wait(lambda: db.execute(
            "SELECT a.status FROM messages m JOIN active_turns a ON a.id=m.active_turn_id WHERE m.id='input-2'"
        ).fetchone()[0] == 'completed')
        turns = db.execute('SELECT status FROM active_turns ORDER BY turn_order').fetchall()
        assert [row[0] for row in turns] == ['completed', 'completed', 'completed']
        rows = db.execute("SELECT id,active_turn_id FROM messages WHERE input_order IS NOT NULL AND role='user' ORDER BY input_order").fetchall()
        assert [row['id'] for row in rows] == ['input-0', 'input-1', 'input-2']
        assert len({row['active_turn_id'] for row in rows}) == 3
    finally:
        (tmp_path / 'release').touch()
        if effect is not None and not effect.done():
            # Base-only deadlock cleanup: unblock its recursive successor drive
            # so the regression reports its assertion instead of hanging pytest.
            _LIVE.pop('move-thread', None)
            lock = _LIVE.existing_lock('move-thread')
            if lock is not None and lock.locked():
                lock.release()
            await asyncio.wait({effect}, timeout=5)
            if effect.done():
                effect.exception()
            else:
                effect.cancel()
        live = _LIVE.get('move-thread')
        if live is not None and live.session is not None:
            await live.session.terminate()
        _LIVE.reset()
        db.close()


def _codex_cli(tmp_path):
    binary = tmp_path / 'stub-codex'
    binary.write_text(f'''#!{sys.executable}
import json, os, sys, time
from pathlib import Path
thread = "provider-" + str(os.getpid())
rollout = Path(os.environ["CODEX_HOME"]) / (thread + ".jsonl")
rollout.write_text(json.dumps({{"type":"session_meta","payload":{{"id":thread}}}}) + "\\n")
number = 0
def emit(value):
    print(json.dumps(value), flush=True)
for line in sys.stdin:
    request = json.loads(line)
    method = request.get("method")
    if "id" not in request:
        continue
    result = {{}}
    if method in ("thread/start", "thread/resume"):
        result = {{"thread": {{"id": thread, "path": str(rollout)}}, "model": "gpt-5-codex"}}
    if method == "turn/start":
        number += 1
        turn = "provider-turn-" + str(number)
        result = {{"turn": {{"id": turn}}}}
    emit({{"id": request["id"], "result": result}})
    if method == "turn/start":
        time.sleep(0.05)
        emit({{"method": "item/completed", "params": {{"threadId": thread, "turnId": turn,
              "item": {{"id": "assistant-" + turn, "type": "agentMessage", "text": "done"}}}}}})
        emit({{"method": "turn/completed", "params": {{"threadId": thread,
              "turn": {{"id": turn, "status": "completed"}}}}}})
''')
    binary.chmod(0o755)
    return str(binary)


@pytest.mark.asyncio
@pytest.mark.parametrize('provider', ['claude-code', 'codex'])
async def test_pinned_idle_move_replaces_old_account_process_at_next_feed(
    isolated_db, signed_in_defaults, monkeypatch, tmp_path, provider,
):
    from connections.account_selection import rebind_idle_account
    from infra.database import get_db
    from llm.resolution import resolve_execution_target
    from subagents import cc_runtime, codex_runtime

    from connections import account_moves

    receipts = []
    monkeypatch.setattr(account_moves, '_publish', receipts.append)
    runtime = cc_runtime if provider == 'claude-code' else codex_runtime
    launch_locks = []
    spawn = asyncio.create_subprocess_exec

    async def observe_spawn(*args, **kwargs):
        launch_locks.append(runtime._LIVE.lock('move-thread').locked())
        return await spawn(*args, **kwargs)

    monkeypatch.setattr(asyncio, 'create_subprocess_exec', observe_spawn)
    binary = _gated_cli(tmp_path) if provider == 'claude-code' else _codex_cli(tmp_path)
    model = 'claude-code/sonnet' if provider == 'claude-code' else 'codex/gpt-5-codex'
    monkeypatch.setattr('connections.models.get_connection', lambda *_a, **_k: {'status': 'available', 'config': {'binary': binary}})
    monkeypatch.setattr('infra.connection_limit_drive.schedule_limited_heads', lambda *_: None)
    monkeypatch.setattr('infra.unavailable_model_recovery.schedule_unavailable_model_recovery', lambda: None)
    db = get_db()
    db.execute("INSERT INTO threads(id,user_id,model,created_at,updated_at) "
               "VALUES ('move-thread','user-1',?,'t','t')", (model,))
    db.commit()
    _, second = _accounts(db, provider)
    target = resolve_execution_target(model, db=db)
    try:
        await _execute(db, _admit(db, 'warmup', 0).batch, target)
        assert await _wait(lambda: db.execute("SELECT status FROM active_turns WHERE id='turn-0'").fetchone()[0] == 'completed')
        old = (runtime._APP_SESSIONS['move-thread'] if provider == 'codex'
               else runtime._LIVE.get('move-thread').session)
        assert old.returncode is None
        moved = rebind_idle_account('move-thread', second['id'], expected_runtime_epoch=0, user_id='user-1', db=db)
        assert moved['changed'] and not runtime._LIVE.lock('move-thread').locked()
        await account_moves._post_effects(receipts[0])
        assert old.returncode is None
        await _execute(db, _admit(db, 'after-pinned-move', 1).batch, target)
        current = (runtime._APP_SESSIONS['move-thread'] if provider == 'codex'
                   else runtime._LIVE.get('move-thread').session)
        assert current is not old and old.returncode is not None
        assert current.vendor_account_id == second['id']
        assert launch_locks == [True, True]
        assert await _wait(lambda: db.execute("SELECT status FROM active_turns WHERE id='turn-1'").fetchone()[0] == 'completed')
    finally:
        live = runtime._LIVE.pop('move-thread', None)
        session = live.session if live is not None else None
        if provider == 'codex':
            session = runtime._APP_SESSIONS.pop('move-thread', None) or session
        if session is not None:
            await asyncio.wait_for(session.terminate(), timeout=3)
        if provider == 'codex':
            runtime._CODEX_THREADS.pop('move-thread', None)
        db.close()
