"""Restore is one server operation: read the version, compare-and-swap, record it with a server-built reason."""
import hashlib
import json
import re

import pytest

from api import files
from doceditor.document_save import DocumentSave, save_document_text
from infra.database import get_db
from tests.test_guidance_history import history_scope as history_scope, SKILL_BODY, _git, _save

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
    # An unregistered file in a thread's tray is never recorded.
    plain = workspace / '.threads' / thread_id / 'scratch' / 'notes.md'
    plain.parent.mkdir(parents=True)
    plain.write_text('notes')
    response = await files.restore_version('test', files.RestoreBody(
        path=str(plain), version='a' * 40, base_sha256=sha('notes'), thread_id=thread_id))
    assert refusal(response) == (409, 'history_off')
    assert plain.read_text() == 'notes'


@pytest.mark.asyncio
@pytest.mark.parametrize('failure', ['history-off', 'repository-fault', 'deferred-lock', 'commit-failure'])
async def test_failures_leave_bytes_unchanged_and_never_say_saved(skill, monkeypatch, failure):
    """Only a state with no history of its own is history off; a Git fault finding the repository fails
    with a Retry that can work, like any other Git fault."""
    from infra import guidance_git
    from infra.guidance_states import HistoryOff

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
        monkeypatch.setattr(guidance_git, 'workspace_repository',
                            lambda *_args, **_kwargs: (None, HistoryOff('the workspace is inside an enclosing repository')))
    elif failure == 'repository-fault':
        monkeypatch.setattr(guidance_git, 'workspace_repository', lambda *_args, **_kwargs: (None, 'git rev-parse timed out'))
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


@pytest.mark.asyncio
async def test_restore_uses_the_bytes_on_disk_not_their_translated_text(skill):
    """The document API revises a file by its bytes, so an instruction file saved with CRLF endings
    restores the way an LF one does and its own line endings come back with it."""
    workspace, thread_id, _skill_path, user_id = skill
    guide = workspace / "notes" / "guide.md"
    guide.parent.mkdir(parents=True)
    db = get_db()
    try:
        db.execute("UPDATE threads SET bootloader_files=? WHERE id=?", ('["notes/guide.md"]', thread_id))
        db.commit()
    finally:
        db.close()
    crlf = "# Guide\r\n\r\nFirst text.\r\n"
    await save_document_text(
        DocumentSave(path=str(guide), text=crlf, thread_id=thread_id, create=True), user_id)
    first = _git(["log", "-1", "--format=%H", "--", "notes/guide.md"], cwd=workspace).strip()
    current = crlf + "Later text.\r\n"
    await save_document_text(DocumentSave(path=str(guide), text=current, thread_id=thread_id), user_id)
    assert guide.read_bytes() == current.encode()

    result = await files.restore_version('test', files.RestoreBody(
        path=str(guide), version=first, base_sha256=sha(current), thread_id=thread_id))

    assert guide.read_bytes() == crlf.encode()
    assert result == {
        "version": _git(["rev-parse", "HEAD"], cwd=workspace).strip(),
        "sha256": sha(crlf),
    }


@pytest.mark.parametrize("state", ["enclosing", "empty-marker"])
@pytest.mark.asyncio
async def test_a_workspace_with_no_repository_of_its_own_is_history_off(skill, state):
    """Both permanent repository states read as history off, the way a guidance save records nothing for
    them: no versions, no history offered on open, and a restore that says so instead of asking for a
    retry that can only repeat itself."""
    from api.files_load import read_document
    from infra.document_history import list_versions

    workspace, thread_id, path, user_id = skill
    first = await recorded(skill, SKILL_BODY)
    current = SKILL_BODY + "\nCurrent text.\n"
    await recorded(skill, current)
    enclosing = workspace.parent
    _git(["init", "--quiet"], cwd=enclosing)
    (workspace / ".git").rename(enclosing / "moved.git")
    if state == "empty-marker":
        (workspace / ".git").mkdir()  # an empty marker: Git answers with the enclosing repository

    assert list_versions(workspace, path.relative_to(workspace).as_posix()) == ([], False)
    # The open's own read asks no Git; the route decides history, off the database pool.
    assert read_document(str(path), user_id, None, thread_id)["_recorded"] is True
    assert (await files.content("test", str(path), thread_id=thread_id))["history"] is False
    assert refusal(await restore(skill, first, sha(current))) == (409, "history_off")
    assert path.read_text() == current


@pytest.mark.parametrize("reason, outcome", [
    ("enclosing", "history-off"), ("no-repository", "history-off"), ("git-fault", "failed"),
])
def test_a_guidance_save_calls_history_off_only_what_the_read_does(history_scope, monkeypatch, reason, outcome):
    """The save and the read share one classification: a Git fault finding the repository is a failed
    save the next try may clear, never history off."""
    from infra import guidance_git
    from infra.guidance_states import HistoryOff, NoRepository

    workspace, _thread = history_scope
    why = {"enclosing": HistoryOff("the workspace is inside an enclosing repository"),
           "no-repository": NoRepository("the workspace has no repository yet"),
           "git-fault": "git rev-parse timed out"}[reason]
    monkeypatch.setattr(guidance_git, "workspace_repository", lambda *_args, **_kwargs: (None, why))
    context = workspace / "context" / "demo.md"
    assert _save(workspace, context, "First text.\n").outcome == outcome


@pytest.mark.asyncio
async def test_an_invalid_skill_version_refuses_with_its_own_kind(skill):
    """Retrying cannot make an invalid SKILL.md valid, so the refusal carries its own kind."""
    workspace, _thread, path, _user = skill
    broken = "---\nname: demo-skill\n---\n\n# Demo skill\n"
    path.write_text(broken)  # an outside edit; the sweep records it as a version of its own
    current = await recorded(skill, SKILL_BODY)
    broken_version = _git(
        ["log", "--format=%H", "--", str(path.relative_to(workspace))], cwd=workspace
    ).split()[-1]

    assert refusal(await restore(skill, broken_version, sha(SKILL_BODY))) == (422, "invalid_skill")
    assert path.read_text() == SKILL_BODY


@pytest.mark.asyncio
async def test_a_read_only_file_refuses_with_its_own_kind(skill):
    """A file the document API never saves says that, instead of a failure line with a Retry."""
    workspace, thread_id, _path, _user = skill
    context = workspace / "context" / "demo.md"
    assert _save(workspace, context, "First text.\n").outcome == "written"
    assert _save(workspace, context, "Second text.\n").outcome == "written"
    first = _git(["log", "--format=%H", "--", "context/demo.md"], cwd=workspace).split()[-1]

    response = await files.restore_version('test', files.RestoreBody(
        path=str(context), version=first, base_sha256=sha("Second text.\n"), thread_id=thread_id))

    assert refusal(response) == (403, "context_read_only")
    assert context.read_text() == "Second text.\n"


def test_document_open_says_whether_the_file_is_recorded(history_scope):
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
    plain = workspace / '.threads' / thread_id / 'scratch' / 'notes.md'
    plain.parent.mkdir(parents=True)
    plain.write_text('notes')
    assert read_document(str(path), user_id, None, thread_id)['_recorded'] is True
    # Opened with no thread, the tray file stays unregistered (a thread's open registers it, and records it).
    assert read_document(str(plain), user_id, None, None)['_recorded'] is False


@pytest.mark.asyncio
async def test_document_open_asks_git_only_off_the_database_pool(skill, monkeypatch):
    """Opening a document runs no Git on the database pool; the route asks once, off it."""
    from api import files_load
    from infra import db_async, document_history

    _workspace, thread_id, path, user_id = skill
    asked = []
    monkeypatch.setattr(document_history, 'history_off', lambda workspace: asked.append(workspace) or False)
    files_load.read_document(str(path), user_id, None, thread_id)
    assert asked == []
    off_db = []
    real_off_db = db_async.run_off_db

    async def counted(fn, *args, **kwargs):
        off_db.append(fn)
        return await real_off_db(fn, *args, **kwargs)

    monkeypatch.setattr(files, 'run_off_db', counted)
    assert (await files.content('test', str(path), thread_id=thread_id))['history'] is True
    assert len(asked) == 1 and off_db == [document_history.history_offered]
