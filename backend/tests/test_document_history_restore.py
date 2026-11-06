"""Restore is one server operation: read the version, compare-and-swap, record it with a server-built reason."""
import hashlib
import json
import re

import pytest

from api import files
from doceditor.document_save import DocumentSave, save_document_text
from infra.database import get_db
from tests.test_guidance_history import history_scope as history_scope, SKILL_BODY, _git

REASON = re.compile(r"Reason: Restored .+’s version of [A-Z][a-z]{2} \d{1,2}, \d{4} at \d{1,2}:\d{2} [AP]M \S+\.")


def sha(text: str) -> str:
    return hashlib.sha256(text.encode()).hexdigest()


@pytest.fixture
def skill(history_scope, monkeypatch):
    """A recorded skill with two versions; yields (workspace, thread, path, user, first version id)."""
    workspace, thread_id = history_scope
    path = workspace / '.threads' / thread_id / 'skills' / 'demo' / 'SKILL.md'
    path.parent.mkdir(parents=True)
    path.write_text(SKILL_BODY)
    db = get_db()
    try:
        user_id = db.execute('SELECT user_id FROM threads WHERE id=?', (thread_id,)).fetchone()[0]
    finally:
        db.close()
    monkeypatch.setattr(files, 'authenticate', lambda _token: {'id': user_id})
    return workspace, thread_id, path, user_id


async def recorded(skill, text: str) -> str:
    workspace, thread_id, path, user_id = skill
    await save_document_text(DocumentSave(path=str(path), text=text, thread_id=thread_id), user_id)
    return _git(['log', '-1', '--format=%H', '--', str(path)], cwd=workspace).strip()


async def restore(skill, version: str, base: str):
    _workspace, thread_id, path, _user = skill
    return await files.restore_version('test', files.RestoreBody(
        path=str(path), version=version, base_sha256=base, thread_id=thread_id))


def refusal(response) -> tuple[int, str]:
    return response.status_code, json.loads(response.body)['kind']


def commits(workspace) -> int:
    return int(_git(['rev-list', '--count', 'HEAD'], cwd=workspace))


@pytest.mark.asyncio
async def test_restore_records_one_version_and_a_retry_is_stale(skill):
    workspace, _thread, path, _user = skill
    first = await recorded(skill, SKILL_BODY)
    current = SKILL_BODY + '\nCurrent text.\n'
    await recorded(skill, current)
    before = commits(workspace)
    result = await restore(skill, first, sha(current))
    assert path.read_text() == SKILL_BODY
    assert commits(workspace) == before + 1
    assert result == {'version': _git(['rev-parse', 'HEAD'], cwd=workspace).strip(), 'sha256': sha(SKILL_BODY)}
    assert REASON.search(_git(['log', '-1', '--format=%B'], cwd=workspace))
    # A lost response retried with the same base never writes twice.
    assert refusal(await restore(skill, first, sha(current))) == (409, 'stale')
    assert path.read_text() == SKILL_BODY
    assert commits(workspace) == before + 1


@pytest.mark.asyncio
async def test_agent_edit_after_display_is_stale(skill):
    workspace, thread_id, path, user_id = skill
    first = await recorded(skill, SKILL_BODY)
    agent_text = SKILL_BODY + '\nAgent edit.\n'
    await save_document_text(DocumentSave(path=str(path), text=agent_text, thread_id=thread_id), user_id, by_agent=True)
    before = commits(workspace)
    assert refusal(await restore(skill, first, sha(SKILL_BODY))) == (409, 'stale')
    assert path.read_text() == agent_text
    assert commits(workspace) == before


@pytest.mark.asyncio
async def test_typing_during_restore_is_its_own_version(skill):
    workspace, _thread, path, _user = skill
    first = await recorded(skill, SKILL_BODY)
    current = SKILL_BODY + '\nCurrent text.\n'
    await recorded(skill, current)
    restored = (await restore(skill, first, sha(current)))['version']
    typed = await recorded(skill, current + 'Typed while restoring.\n')
    log = _git(['log', '--format=%H', '--', str(path)], cwd=workspace).split()
    assert log[:2] == [typed, restored]
    assert _git(['show', f'{restored}:{path.relative_to(workspace)}'], cwd=workspace) == SKILL_BODY


@pytest.mark.asyncio
async def test_unknown_version_is_unavailable(skill):
    _workspace, _thread, path, _user = skill
    await recorded(skill, SKILL_BODY)
    assert refusal(await restore(skill, '0' * 40, sha(SKILL_BODY))) == (404, 'version_unavailable')
    assert refusal(await restore(skill, '--help', sha(SKILL_BODY))) == (404, 'version_unavailable')
    assert path.read_text() == SKILL_BODY


@pytest.mark.asyncio
async def test_file_without_history_is_history_off(skill):
    workspace, thread_id, _path, _user = skill
    plain = workspace / 'notes.md'
    plain.write_text('notes')
    response = await files.restore_version('test', files.RestoreBody(
        path=str(plain), version='a' * 40, base_sha256=sha('notes'), thread_id=thread_id))
    assert refusal(response) == (409, 'history_off')
    assert plain.read_text() == 'notes'


@pytest.mark.asyncio
@pytest.mark.parametrize('failure', ['history-off', 'deferred-lock', 'commit-failure'])
async def test_failures_leave_bytes_unchanged_and_never_say_saved(skill, monkeypatch, failure):
    from infra import guidance_git

    workspace, _thread, path, _user = skill
    first = await recorded(skill, SKILL_BODY)
    current = SKILL_BODY + '\nCurrent text.\n'
    await recorded(skill, current)
    before = commits(workspace)

    def fail(exc):
        def raise_error(*_args, **_kwargs):
            raise exc
        return raise_error

    if failure == 'history-off':
        monkeypatch.setattr(guidance_git, 'workspace_repository', lambda *_args, **_kwargs: (None, 'history off'))
    elif failure == 'deferred-lock':
        monkeypatch.setattr(guidance_git, '_acquire', fail(guidance_git._LockBusy('lock busy')))
    else:
        monkeypatch.setattr(guidance_git, '_commit', fail(guidance_git.GitFault('commit failed')))
    response = await restore(skill, first, sha(current))
    expected = (409, 'history_off') if failure == 'history-off' else (503, 'failed')
    assert refusal(response) == expected
    assert 'saved' not in json.loads(response.body)['detail'].lower()
    assert path.read_text() == current
    assert commits(workspace) == before


def test_document_open_says_whether_history_exists(history_scope):
    from api.files_load import read_document

    workspace, thread_id = history_scope
    db = get_db()
    try:
        user_id = db.execute('SELECT user_id FROM threads WHERE id=?', (thread_id,)).fetchone()[0]
    finally:
        db.close()
    path = workspace / '.threads' / thread_id / 'skills' / 'demo' / 'SKILL.md'
    path.parent.mkdir(parents=True)
    path.write_text(SKILL_BODY)
    plain = workspace / 'notes.md'
    plain.write_text('notes')
    assert read_document(str(path), user_id, None, thread_id)['history'] is True
    assert read_document(str(plain), user_id, None, thread_id)['history'] is False
