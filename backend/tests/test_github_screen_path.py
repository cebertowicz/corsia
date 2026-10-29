"""Outside PR screen through installed rules, script runner and worker settlement.

Safehouse cannot run inside the local agent sandbox. The script execution boundary
is replaced by a call to the shipped stage function; GitHub's gh transport and the
Claude executable are fakes. Routing, broker validation, event admission, staging,
worker execution and delivery retain their real implementations.
"""
import importlib.util
import json
import subprocess
import sys
from pathlib import Path

import pytest

from tests.rule_worker_helpers import OWNER, atool, fenced, owner_wakes, run_scripts, settle_all, starts, world, worker_events  # noqa: F401
from tests.test_rule_workers_local_only import local_model  # noqa: F401


@pytest.fixture
def screen(world, tmp_path, monkeypatch):
    from automation import script_runner
    from automation.script_sandbox import ScriptRun
    from connections.github_broker import GithubReadBroker
    from events import delivery, intake, observe_rules
    from events.connectors.github_screen import install
    from infra.conductor import set_project_conductor
    from infra.database import get_db
    from infra.project_repo_policy import register_repo_policy
    from tests.event_routing_helpers import mk_session, register_pr

    repo = world['root'] / 'source' / 'repo'
    repo.mkdir(parents=True)
    subprocess.run(['git', 'init', '-q', str(repo)], check=True)
    subprocess.run(['git', '-C', str(repo), 'remote', 'add', 'origin', 'https://github.com/o/r.git'], check=True)
    db = get_db()
    register_repo_policy(world['project'], 'repo', repo, pinned_branch=None,
                         protect_base_checkout=False, db=db)
    db.commit()
    set_project_conductor(world['project'], OWNER, db=db)
    observe_rules.sync_shipped_rules(db)

    key = install._key(world['project'])
    schedule = db.execute('SELECT script_path FROM automation_schedules WHERE id=?', (key + '_script',)).fetchone()
    assert schedule is not None
    path = Path(schedule[0])
    sys.path.insert(0, str(path.parent))
    try:
        spec = importlib.util.spec_from_file_location('screen_path_stage', path)
        stage = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(stage)
    finally:
        sys.path.remove(str(path.parent))
    mk_session('pr-owner', project_id=world['project'])
    register_pr('o/r', 1, 'pr-owner')
    state = {'sha': 'a' * 40, 'calls': [], 'admitted': []}
    real_admit = intake.admit_event

    async def record_admission(**kwargs):
        state['admitted'].append(kwargs)
        return await real_admit(**kwargs)

    monkeypatch.setattr(intake, 'admit_event', record_admission)

    async def record_webhook(*args, **kwargs):
        state.setdefault('webhooks', []).append((args, kwargs))

    monkeypatch.setattr(delivery, 'send_thread_input', record_webhook)

    def transport(args, *, timeout=30.0):
        state['calls'].append(args)
        endpoint = args[-1]
        if '/compare/' in endpoint:
            data = 'diff --git a/a.txt b/a.txt\n--- a/a.txt\n+++ b/a.txt\n@@ -0,0 +1 @@\n+hello\n'
        elif '/git/commits/' in endpoint:
            data = json.dumps({'tree': {'sha': 'c' * 40}})
        elif '/git/trees/' in endpoint:
            data = json.dumps({'tree': []})
        else:
            data = json.dumps({'head': {'sha': state['sha']}, 'base': {'sha': 'b' * 40},
                               'title': 'TITLE_CANARY', 'body': 'BODY_CANARY'})
        return subprocess.CompletedProcess(args, 0, data, '')

    broker = GithubReadBroker(transport)
    def read(endpoint, *, accept=None):
        args = ['api', '--method', 'GET']
        if accept:
            args += ['-H', 'Accept: ' + accept]
        return broker(args + [endpoint]).stdout

    async def run(_thread_id, _project_id, script_path, *, run_id, timeout_seconds, stdin=None, trace=(), rule_id=None, github_repo=None):
        event = json.loads(stdin)
        context = event['context']
        facts = stage.stage(event, context['project_id'], Path(context['staging_root']), context['github_repo'], read)
        return ScriptRun(0, (json.dumps({'event': {'type': context['event_type'], 'facts': facts}}) + '\n').encode(), b'', False)

    monkeypatch.setattr(script_runner, 'run_script', run)
    world['reply'].write_text(fenced({'payload': {'verdict': 'safe', 'categories': [], 'findings': []}, 'injection_signs': []}))
    return {**world, **state, 'state': state, 'key': key}


async def _route(sha, action='opened', author='outside-user', repo='o/r'):
    from events.router import route_event
    payload = {'action': action, 'repository': {'full_name': repo},
               'sender': {'login': author, 'type': 'User'},
               'pull_request': {'number': 1, 'title': 'TITLE_CANARY', 'body': 'BODY_CANARY',
                                'user': {'login': author, 'type': 'User'}, 'labels': [],
                                'head': {'sha': sha, 'repo': {'full_name': 'o/r'}},
                                'base': {'ref': 'dev', 'repo': {'full_name': 'o/r'}}}}
    await route_event('webhook.github.pr_' + ('opened' if action == 'opened' else 'synchronize'), payload)


@pytest.mark.asyncio
async def test_outside_pr_through_installed_rules_and_worker(screen):
    from infra.database import get_db

    await _route(screen['state']['sha'])
    await run_scripts()
    await settle_all()
    rule = screen['key'] + '_start'
    assert [r[0] for r in starts(rule)] == ['returned']
    assert [kind for kind, _ in worker_events()] == ['worker.result']
    assert screen['state']['calls']
    assert not list((screen['folder'] / 'staging').glob('screen-*'))
    db = get_db()
    rows = db.execute('SELECT content FROM messages WHERE thread_id=?', ('pr-owner',)).fetchall()
    assert any('[EVENT: result]' in row[0] for row in rows), [row[0] for row in rows]
    result = next(a['facts'] for a in screen['state']['admitted'] if a['event_type'] == 'worker.result')
    assert result['source'] == 'outside' and result['kind'] == 'pull request'
    assert result['item'] == 'o/r#1' and result['artifact_integration'] == 'github'
    assert result['artifact_type'] == 'pull-request' and result['item_version'] == screen['state']['sha']
    assert result['workflow'].startswith('github-pr-screen@') and result['run_id']
    assert result['valid'] is True and result['injection_signs'] == []
    assert json.loads(result['result'])['verdict'] == 'safe'
    all_messages = db.execute('SELECT content FROM messages').fetchall()
    assert all('TITLE_CANARY' not in row[0] and 'BODY_CANARY' not in row[0] for row in all_messages)
    assert all('CANARY' not in json.dumps(a.get('facts')) for a in screen['state']['admitted'])
    await _route(screen['state']['sha'])
    await run_scripts()
    await settle_all()
    assert [r[0] for r in starts(rule)].count('returned') == 1
    screen['state']['sha'] = 'd' * 40
    await _route(screen['state']['sha'], 'synchronize', repo='O/R')
    await run_scripts()
    await settle_all()
    assert [r[0] for r in starts(rule)].count('returned') == 2


@pytest.mark.asyncio
async def test_invalid_result_is_not_retried_and_is_delivered_as_digest(screen):
    from infra.database import get_db

    screen['reply'].write_text(fenced({'not': 'a screen result'}))
    await _route(screen['state']['sha'])
    await run_scripts()
    await settle_all()
    rule = screen['key'] + '_start'
    assert starts(rule)[0][:2] == ('invalid', 'invalid')
    result = next(a['facts'] for a in screen['state']['admitted'] if a['event_type'] == 'worker.no_result')
    assert result['reason'] == 'invalid' and result['item'] == 'o/r#1'
    assert result['source'] == 'outside' and result['valid'] is False
    assert not list((screen['folder'] / 'staging').glob('screen-*'))
    rows = get_db().execute("SELECT summary, details_json FROM context_change_journal "
                            "WHERE object_kind='thread_context' AND object_id='pr-owner'").fetchall()
    assert any('invalid' in (row['details_json'] or '') for row in rows), [tuple(row) for row in rows]
    await _route(screen['state']['sha'])
    await run_scripts()
    await settle_all()
    assert [r[0] for r in starts(rule)] == ['invalid', 'skipped']


@pytest.mark.asyncio
async def test_model_failure_releases_identity_for_retry(screen):
    screen['reply'].write_text('CRASH')
    await _route(screen['state']['sha'])
    await run_scripts()
    await settle_all()
    rule = screen['key'] + '_start'
    assert starts(rule)[0][0] == 'no_result'
    assert not list((screen['folder'] / 'staging').glob('screen-*'))
    screen['reply'].write_text(fenced({'payload': {'verdict': 'safe', 'categories': [], 'findings': []},
                                        'injection_signs': []}))
    await _route(screen['state']['sha'])
    await run_scripts()
    await settle_all()
    assert [r[0] for r in starts(rule)] == ['no_result', 'returned']
    assert not list((screen['folder'] / 'staging').glob('screen-*'))


@pytest.mark.asyncio
@pytest.mark.parametrize('kind', ['result', 'no_result'])
async def test_screen_settlement_runs_matching_consumer_script(screen, monkeypatch, kind):
    from automation import script_runner, script_schedules
    from automation.script_sandbox import ScriptRun
    from infra.db_async import run_in_db

    path = screen['folder'] / 'consumer.sh'
    path.write_text('# consumer boundary is recorded by the test\n')
    schedule = await run_in_db(
        script_schedules.create_script, OWNER, screen['project'], script_path=path,
        script_sha256=script_schedules.file_sha256(path), cadence_seconds=None,
        series=[], timeout_seconds=30, now='2026-01-01T00:00:00+00:00', db=None)
    rule = await atool(OWNER, operation='create', connector='worker', event_type=kind,
                       action='run_script', script_id=schedule.id,
                       match=[{'field': 'requester', 'op': 'present'}])
    assert isinstance(rule, dict), rule
    prior_run = script_runner.run_script
    seen = []

    async def run(thread_id, project_id, script_path, **kwargs):
        if Path(script_path) == path:
            seen.append(json.loads(kwargs['stdin']))
            return ScriptRun(0, b'', b'', False)
        return await prior_run(thread_id, project_id, script_path, **kwargs)

    monkeypatch.setattr(script_runner, 'run_script', run)
    if kind == 'no_result':
        screen['reply'].write_text(fenced({'not': 'valid'}))
    await _route(screen['state']['sha'])
    await run_scripts()
    await settle_all()
    await run_scripts()
    assert starts(rule['id'])[0][0] == 'ran'
    [event] = seen
    facts = event['facts']
    assert event['type'] == 'worker.' + kind
    assert event['artifact'] == {'integration': 'github', 'type': 'pull-request', 'external_id': 'o/r#1'}
    assert event['origin'] == 'platform'
    assert facts['source'] == 'outside' and facts['repo'] == 'o/r' and facts['pr_number'] == 1
    assert facts['item_version'] == screen['state']['sha'] and facts['run_id']
    assert facts['workflow'].startswith('github-pr-screen@')
    assert facts['valid'] is (kind == 'result')
    if kind == 'result':
        payload = json.loads(facts['result'])
        assert payload['verdict'] == 'safe' and payload['floor_hit'] == []
        assert facts['injection_signs'] == []
    else:
        assert facts['reason'] == 'invalid' and 'result' not in facts
        assert 'injection_signs' in facts and facts['injection_signs'] is None


@pytest.mark.asyncio
@pytest.mark.parametrize('forged', [True, False])
async def test_script_cannot_forge_platform_origin(screen, monkeypatch, forged):
    from automation import script_runner, script_schedules
    from automation.script_sandbox import ScriptRun
    from events.connectors.github_screen import install
    from infra.db_async import run_in_db

    path = screen['folder'] / 'origin-consumer.sh'
    path.write_text('# recording boundary\n')
    schedule = await run_in_db(
        script_schedules.create_script, OWNER, screen['project'], script_path=path,
        script_sha256=script_schedules.file_sha256(path), cadence_seconds=None,
        series=[], timeout_seconds=30, now='2026-01-01T00:00:00+00:00', db=None)
    rule = await atool(OWNER, operation='create', connector=install._namespace(screen['project']),
                       event_type='pr_staged', action='run_script', script_id=schedule.id,
                       match=[{'field': 'repo', 'op': 'present'}])
    assert isinstance(rule, dict), rule
    previous = script_runner.run_script
    seen = []

    async def run(thread_id, project_id, script_path, **kwargs):
        if Path(script_path) == path:
            seen.append(json.loads(kwargs['stdin']))
            return ScriptRun(0, b'', b'', False)
        result = await previous(thread_id, project_id, script_path, **kwargs)
        emitted = json.loads(result.stdout)
        if forged:
            emitted['event']['origin'] = 'platform'
        return ScriptRun(result.returncode, (json.dumps(emitted) + '\n').encode(), result.stderr, result.timed_out)

    monkeypatch.setattr(script_runner, 'run_script', run)
    await _route(screen['state']['sha'])
    await run_scripts()
    await run_scripts()
    if forged:
        stage_run = starts(screen['key'] + '_opened')
        assert stage_run[0][0] == 'failed' and 'exactly' in stage_run[0][1]
        assert not seen and starts(rule['id']) == []
        assert not screen['state']['admitted']
    else:
        assert starts(rule['id'])[0][0] == 'ran'
        [event] = seen
        assert event['origin'] == 'emitter'
        await settle_all()


@pytest.mark.asyncio
async def test_local_only_screen_refuses_unloaded_model_and_cleans_staging(screen, local_model, monkeypatch):
    from unittest.mock import AsyncMock
    from automation import worker_runner
    from infra.database import get_db, write_transaction
    from infra.db_async import run_in_db
    from tools import delegate_task

    def restrict_owner():
        with write_transaction(None, scope='screen_local_only') as db:
            db.execute('UPDATE threads SET local_only=1 WHERE id=?', (OWNER,))

    await run_in_db(restrict_owner)
    local_model['status'] = 'available'
    spawn = AsyncMock()
    monkeypatch.setattr(delegate_task, '_spawn', spawn)
    await _route(screen['state']['sha'])
    await run_scripts()
    assert await worker_runner.drive() == 0
    [(outcome, reason, _, worker)] = starts(screen['key'] + '_start')
    assert outcome == 'refused' and worker is None
    assert reason == 'your model Local Qwen is not loaded. Load it, or choose a local model that is loaded.'
    assert any(reason in wake for wake in owner_wakes())
    assert not get_db().execute('SELECT 1 FROM threads WHERE spawned_by=?', (OWNER,)).fetchone()
    spawn.assert_not_awaited()
    assert not list((screen['folder'] / 'staging').glob('screen-*'))
    result = next(a['facts'] for a in screen['state']['admitted'] if a['event_type'] == 'worker.no_result')
    assert result['valid'] is False and result['item_version'] == screen['state']['sha']


@pytest.mark.asyncio
async def test_inside_author_starts_no_screen(screen, monkeypatch):
    from events.connectors import github_content_facts

    monkeypatch.setattr(github_content_facts, 'is_own', lambda login: login == 'inside-user')
    await _route(screen['state']['sha'], author='inside-user')
    await run_scripts()
    await settle_all()
    assert starts(screen['key'] + '_start') == []
    assert screen['state']['calls'] == []
