"""Outside issue and comment triage through shipped rules and worker settlement."""
import importlib.util
import json
import sys
from pathlib import Path

import pytest

from tests.rule_worker_helpers import fenced, run_scripts, settle_all, starts, world  # noqa: F401
from tests.test_github_screen_path import screen, _route  # noqa: F401
from tests.test_rule_workers_local_only import local_model  # noqa: F401


@pytest.fixture
def triage(screen, monkeypatch):
    from automation import script_runner
    from automation.script_sandbox import ScriptRun
    from events.connectors.github_screen.workflow import workflow

    folder = screen['folder'] / 'github-screen' / screen['key']
    path = folder / 'stage.py'
    sys.path.insert(0, str(folder))
    try:
        spec = importlib.util.spec_from_file_location('triage_test_stage', path)
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
    finally:
        sys.path.remove(str(folder))
    previous = script_runner.run_script
    screen['item'] = {'id': 42, 'number': 1, 'body': 'safe item', 'updated_at': '2026-10-28T01:00:00Z'}
    schema = workflow(screen['project'], 'github-outside-triage').corsia['workflow']['result']
    choices = schema['properties']['payload']['properties']['next']['enum']
    screen['reply'].write_text(fenced({'payload': {'next': choices[0], 'summary': 'A report', 'findings': [], 'refs': []}, 'injection_signs': []}))

    async def run(thread_id, project_id, script_path, **kwargs):
        event = json.loads(kwargs['stdin'])
        context = event['context']
        if not context['event_type'].endswith('.item_staged'):
            return await previous(thread_id, project_id, script_path, **kwargs)
        def read(endpoint, **_kwargs):
            screen['state'].setdefault('triage_reads', []).append(endpoint)
            return json.dumps(screen['item'])
        result = module.stage_item(event, context['project_id'], Path(context['staging_root']), context['github_repo'], read)
        return ScriptRun(0, (json.dumps({'event': {'type': context['event_type'], 'facts': result}}) + '\n').encode(), b'', False)

    monkeypatch.setattr(script_runner, 'run_script', run)
    return screen


def payload(kind, action, *, body='safe item', updated='2026-10-28T01:00:00Z', author='outside-user'):
    item = {'id': 42, 'number': 1, 'body': body, 'updated_at': updated, 'user': {'login': author, 'type': 'User'}}
    result = {'action': action, 'repository': {'full_name': 'o/r'}, 'sender': {'login': author, 'type': 'User'}}
    if kind == 'issue':
        result['issue'] = item
    elif kind in ('issue_comment', 'pr_comment'):
        result['issue'] = {'number': 1, 'user': {'login': 'other'}, **({'pull_request': {'url': 'x'}} if kind == 'pr_comment' else {})}
        result['comment'] = item
    else:
        result['pull_request'] = {'number': 1, 'user': {'login': 'other'}}
        result['review'] = item
    return result


async def route(kind, action, **kwargs):
    from events.github_mapper import map_github_event
    from events.router import route_event
    event = {'issue': 'issues', 'issue_comment': 'issue_comment', 'pr_comment': 'issue_comment', 'review': 'pull_request_review'}[kind]
    raw = payload(kind, action, **kwargs)
    mapped = map_github_event(event, raw)
    return await route_event(mapped, raw)


@pytest.mark.asyncio
@pytest.mark.parametrize('kind,action', [('issue', 'opened'), ('issue_comment', 'created'), ('issue_comment', 'edited'), ('pr_comment', 'created'), ('pr_comment', 'edited'), ('review', 'submitted'), ('review', 'edited')])
async def test_each_type_reaches_stage_and_worker(triage, kind, action):
    await route(kind, action)
    await run_scripts()
    await settle_all()
    suffix = ('issue' if kind == 'issue' else kind) + '_' + action
    assert starts(triage['key'] + '_triage_' + suffix)[0][0] == 'ran'
    assert starts(triage['key'] + '_triage_start')[0][0] == 'returned'
    assert not list((triage['folder'] / 'staging').glob('triage-*'))


@pytest.mark.asyncio
async def test_screen_only_and_repeated_comment_version(triage):
    triage_reply = triage['reply'].read_text()
    triage['reply'].write_text(fenced({'payload': {'verdict': 'safe', 'categories': [], 'findings': []}, 'injection_signs': []}))
    await _route(triage['state']['sha'])
    await run_scripts()
    await settle_all()
    assert starts(triage['key'] + '_start')[0][0] == 'returned'
    assert starts(triage['key'] + '_triage_start') == []
    triage['reply'].write_text(triage_reply)
    for _ in range(2):
        await route('issue_comment', 'edited')
        await run_scripts()
        await settle_all()
    assert [x[0] for x in starts(triage['key'] + '_triage_start')] == ['returned', 'skipped']
    triage['item']['updated_at'] = '2026-10-28T02:00:00Z'
    await route('issue_comment', 'edited', updated=triage['item']['updated_at'])
    await run_scripts()
    await settle_all()
    assert [x[0] for x in starts(triage['key'] + '_triage_start')].count('returned') == 2


@pytest.mark.asyncio
async def test_review_version_dedupes_and_missing_body_is_not_routed(triage):
    for body in ('safe item', 'safe item', 'another item', None):
        triage['item']['body'] = body
        await route('review', 'edited', body=body)
        await run_scripts()
        await settle_all()
    assert [x[0] for x in starts(triage['key'] + '_triage_start')] == ['returned', 'skipped', 'returned']
    assert len(starts(triage['key'] + '_triage_review_edited')) == 3
    from events.connectors.github import MANIFEST
    missing = payload('review', 'edited')
    del missing['review']['body']
    assert 'item_version' not in MANIFEST.extract_facts(missing, None)


@pytest.mark.asyncio
async def test_inside_edit_starts_nothing(triage, monkeypatch):
    from events.connectors import github_content_facts
    monkeypatch.setattr(github_content_facts, 'is_own', lambda login: login == 'inside-user')
    await route('issue_comment', 'edited', author='inside-user')
    await run_scripts()
    assert starts(triage['key'] + '_triage_start') == []


@pytest.mark.asyncio
async def test_invalid_enum_is_final_and_cleanup_after_failed_start(triage):
    triage['reply'].write_text(fenced({'payload': {'next': 'not-a-choice', 'summary': '', 'findings': [], 'refs': []}, 'injection_signs': []}))
    for _ in range(2):
        await route('issue_comment', 'created')
        await run_scripts()
        await settle_all()
    assert [x[:2] for x in starts(triage['key'] + '_triage_start')] == [('invalid', 'invalid'), ('skipped', starts(triage['key'] + '_triage_start')[1][1])]
    assert not list((triage['folder'] / 'staging').glob('triage-*'))
    assert any(a['event_type'] == 'worker.no_result' and a['facts']['reason'] == 'invalid' for a in triage['state']['admitted'])


@pytest.mark.asyncio
async def test_issue_owner_gets_worker_result_not_raw_event(triage):
    from infra.database import get_db
    from registry import models
    from tests.event_routing_helpers import mk_session
    mk_session('issue-owner', project_id=triage['project'])
    models.register_artifact(integration='github', type='issue', external_id='o/r#1', parent_thread_id='issue-owner', title='Issue')
    result = await route('issue', 'opened')
    assert result.thread_id is None
    await run_scripts()
    await settle_all()
    db = get_db()
    row = db.execute('SELECT artifact_integration, artifact_type, artifact_external_id FROM event_route_decisions WHERE rule_id=?', (triage['key'] + '_triage_issue_opened',)).fetchone()
    assert tuple(row) == ('github', 'issue', 'o/r#1')
    messages = db.execute('SELECT content FROM messages WHERE thread_id=?', ('issue-owner',)).fetchall()
    assert any('[EVENT: result]' in row[0] for row in messages)
    assert all('safe item' not in row[0] for row in messages)


@pytest.mark.asyncio
async def test_failed_start_cleans_staging(triage, local_model):
    from automation import worker_runner
    from infra.database import write_transaction
    from infra.db_async import run_in_db
    from tests.rule_worker_helpers import OWNER

    def restrict_owner():
        with write_transaction(None, scope='triage_local_only') as db:
            db.execute('UPDATE threads SET local_only=1 WHERE id=?', (OWNER,))
    await run_in_db(restrict_owner)
    local_model['status'] = 'available'
    result = await route('pr_comment', 'created')
    assert (result.outcome.value, result.reason) == ('parked', 'rule_only')
    await run_scripts()
    assert await worker_runner.drive() == 0
    assert starts(triage['key'] + '_triage_start')[0][0] == 'refused'
    assert any(a['event_type'] == 'worker.no_result' for a in triage['state']['admitted'])
    from infra.database import get_db
    contents = [row[0] for row in get_db().execute('SELECT content FROM messages WHERE thread_id=?', ('pr-owner',))]
    wakes = [content for content in contents if '[EVENT: no_result]' in content]
    assert len(wakes) == 1 and 'not loaded' in wakes[0], contents
    assert not list((triage['folder'] / 'staging').glob('triage-*'))


@pytest.mark.asyncio
async def test_intake_exception_cleans_staging(triage, monkeypatch):
    from events import intake
    original = intake.admit_event
    staged = []

    async def fail_staged(**kwargs):
        if kwargs['event_type'].endswith('.item_staged'):
            staged.append(kwargs['facts']['staging_path'])
            assert (triage['folder'] / 'staging' / staged[-1]).is_dir()
            raise RuntimeError('intake failed')
        return await original(**kwargs)

    monkeypatch.setattr(intake, 'admit_event', fail_staged)
    await route('issue_comment', 'created')
    await run_scripts()
    assert len(staged) == 1
    assert not list((triage['folder'] / 'staging').glob('triage-*'))
    assert starts(triage['key'] + '_triage_start') == []


@pytest.mark.asyncio
async def test_pr_comment_owner_gets_one_result_and_no_raw_event(triage):
    from events.connectors import github
    from infra.database import get_db

    raw = payload('pr_comment', 'created')
    facts = github.MANIFEST.extract_facts(raw, None)
    assert github.MANIFEST.build_artifact_ref('pr_comment.created', facts, raw) == {
        'integration': 'github', 'type': 'pull-request', 'external_id': 'o/r#1'}
    result = await route('pr_comment', 'created')
    assert (result.outcome.value, result.reason, result.thread_id) == ('parked', 'rule_only', None)
    await run_scripts()
    await settle_all()
    contents = [row[0] for row in get_db().execute('SELECT content FROM messages WHERE thread_id=?', ('pr-owner',))]
    assert sum('[EVENT: result]' in content for content in contents) == 1
    assert not any('safe item' in content for content in contents)


@pytest.mark.asyncio
async def test_issue_comment_owner_gets_one_result_and_no_raw_event(triage):
    from infra.database import get_db
    from registry import models
    from tests.event_routing_helpers import mk_session

    mk_session('issue-owner', project_id=triage['project'])
    models.register_artifact(integration='github', type='issue', external_id='o/r#1', parent_thread_id='issue-owner', title='Issue')
    result = await route('issue_comment', 'created')
    assert (result.outcome.value, result.reason, result.thread_id) == ('parked', 'rule_only', None)
    await run_scripts()
    await settle_all()
    contents = [row[0] for row in get_db().execute('SELECT content FROM messages WHERE thread_id=?', ('issue-owner',))]
    assert sum('[EVENT: result]' in content for content in contents) == 1
    assert not any('safe item' in content for content in contents)


@pytest.mark.asyncio
async def test_inside_created_comment_still_routes(triage, monkeypatch):
    from events.connectors import github_content_facts
    monkeypatch.setattr(github_content_facts, 'is_own', lambda login: login == 'inside-user')
    result = await route('pr_comment', 'created', author='inside-user')
    assert (result.outcome.value, result.thread_id) == ('accepted', 'pr-owner')
    await run_scripts()
    assert starts(triage['key'] + '_triage_start') == []


@pytest.mark.asyncio
async def test_empty_submitted_review_starts_nothing(triage):
    for body in (None, ''):
        await route('review', 'submitted', body=body)
        await run_scripts()
    assert starts(triage['key'] + '_triage_review_submitted') == []
    assert starts(triage['key'] + '_triage_start') == []


@pytest.mark.asyncio
async def test_edited_review_with_lone_surrogate_reaches_worker(triage):
    triage['item']['body'] = '\ud800'
    await route('review', 'edited', body='\ud800')
    await run_scripts()
    await settle_all()
    assert starts(triage['key'] + '_triage_start')[0][0] == 'returned'
