"""Every kept text file has history: which saves are recorded as document versions, what a document's save
skips, how the list groups saves, and that history never fails a save (live lane included)."""
import asyncio
import hashlib
import json
import subprocess
import time
from pathlib import Path
from types import SimpleNamespace

import pytest

from doceditor.document_save import DocumentSave, save_document_text
from infra.database import get_db
from infra.document_history import GROUP_GAP, grouped, list_versions
from infra.guidance_targets import history_target
from tests.test_guidance_history import history_scope as history_scope, SKILL_BODY, _git


def sha(text: str) -> str:
    return hashlib.sha256(text.encode()).hexdigest()


def owner(thread_id: str) -> str:
    db = get_db()
    try:
        return db.execute('SELECT user_id FROM threads WHERE id=?', (thread_id,)).fetchone()[0]
    finally:
        db.close()


def placed(path: Path, text: str = 'Notes.\n') -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text)
    return path


def subjects(workspace: Path, path: Path) -> list[str]:
    return _git(['log', '--format=%s', '--', str(path.relative_to(workspace))], cwd=workspace).split('\n')[:-1]


def test_which_saves_are_recorded_as_documents(history_scope):
    from registry import service as registry_service

    workspace, thread_id = history_scope
    tray = workspace / '.threads' / thread_id
    document = placed(workspace / 'notes' / 'plan.md')
    registered = placed(tray / 'reports' / 'summary.md')
    db = get_db()
    try:
        registry_service.register_write_strict(thread_id, str(registered), db=db)
    finally:
        db.close()
    unregistered = placed(tray / 'scratch' / 'notes.md')
    working = placed(workspace / 'projects' / 'demo' / 'working' / 'draft.md')
    instruction = placed(tray / 'skills' / 'demo' / 'SKILL.md', SKILL_BODY)
    ignored = placed(workspace / 'private' / 'secret.md')
    (workspace / '.gitignore').write_text('/private/\n')
    nested = placed(workspace / 'projects' / 'demo' / 'source' / 'repo' / 'README.md')
    (nested.parent / '.git').mkdir()

    assert history_target(document, workspace).document is True
    assert history_target(registered, workspace).document is True
    assert history_target(working, workspace) is None
    assert history_target(unregistered, workspace) is None
    skill = history_target(instruction, workspace)
    assert skill is not None and skill.document is False
    assert history_target(nested, workspace) is None
    # An ignored file is a document; the writer's own ignore check keeps its save unrecorded.
    assert history_target(ignored, workspace).document is True


@pytest.mark.asyncio
async def test_a_document_git_ignores_saves_unrecorded(history_scope):
    workspace, thread_id = history_scope
    (workspace / '.gitignore').write_text('/private/\n')
    path = placed(workspace / 'private' / 'secret.md')
    before = _git(['rev-parse', 'HEAD'], cwd=workspace).strip()
    saved = await save_document_text(DocumentSave(path=str(path), text='Kept out.\n', thread_id=thread_id), owner(thread_id))
    assert path.read_text() == 'Kept out.\n'
    assert saved['version'] is None and 'guidance_history' not in saved
    assert _git(['rev-parse', 'HEAD'], cwd=workspace).strip() == before


@pytest.mark.asyncio
async def test_document_save_skips_the_lesson_check_and_carry_in(history_scope, monkeypatch):
    from core import carry_in_facts
    from infra import lesson_save_gate
    from registry import service as registry_service

    workspace, thread_id = history_scope
    # An agent writes in its own tray; registered, the file is a document.
    path = placed(workspace / '.threads' / thread_id / 'reports' / 'plan.md')
    db = get_db()
    try:
        registry_service.register_write_strict(thread_id, str(path), db=db)
    finally:
        db.close()
    asked: list[str] = []
    monkeypatch.setattr(lesson_save_gate, 'lesson_changes', lambda *_a, **_k: asked.append('lesson') or ())
    monkeypatch.setattr(carry_in_facts, 'record_committed_save', lambda *_a, **_k: asked.append('carry-in'))
    saved = await save_document_text(
        DocumentSave(path=str(path), text='Agent text.\n', thread_id=thread_id), owner(thread_id), by_agent=True)
    assert asked == []
    assert path.read_text() == 'Agent text.\n'
    assert saved['version'] == _git(['rev-parse', 'HEAD'], cwd=workspace).strip()
    assert 'guidance_history' not in saved
    # The text it held before (never recorded) is kept first as its own version, then the save.
    assert subjects(workspace, path) == [
        f'document save: .threads/{thread_id}/reports/plan.md', f'guidance sweep: .threads/{thread_id}/reports/plan.md']
    author = _git(['log', '-1', '--format=%ae'], cwd=workspace).strip()
    assert author == f'{thread_id}@agents.corsia.local'


@pytest.mark.asyncio
async def test_a_history_fault_never_fails_a_document_save(history_scope, monkeypatch):
    from infra import guidance_git

    workspace, thread_id = history_scope
    path = placed(workspace / 'notes' / 'plan.md')
    before = _git(['rev-parse', 'HEAD'], cwd=workspace).strip()
    monkeypatch.setattr(guidance_git, '_commit', lambda *_a, **_k: (_ for _ in ()).throw(guidance_git.GitFault('disk full')))
    saved = await save_document_text(DocumentSave(path=str(path), text='Kept.\n', thread_id=thread_id), owner(thread_id))
    assert path.read_text() == 'Kept.\n'
    assert saved['version'] is None
    assert 'disk full' in saved['guidance_history']
    assert _git(['rev-parse', 'HEAD'], cwd=workspace).strip() == before


def row(author: str, minute: float, reason: str | None = None, commit: str = '') -> dict:
    stamp = f'2026-11-09T10:{int(minute):02d}:{int(minute % 1 * 60):02d}-08:00'
    return {'id': commit or f'{author}-{minute}', 'author': author, 'savedAt': stamp, 'reason': reason}


def test_grouping_joins_one_authors_quick_saves_only():
    # Newest first, as Git lists them.
    rows = [
        row('Ana', 40), row('Ana', 31, 'Tighten intro'), row('Ana', 25),
        row('Ana', 20), row('Bo', 12), row('Bo', 3), row('Ana', 0, 'Start'),
    ]
    shown = grouped(rows)
    # 40 joins 31 under its reason (9 minutes, none stated); 31 starts a group (a reason); 25 joins 20;
    # 20 starts one (a different author before it); 12 joins 3; 0 is the oldest.
    assert [(r['id'], r['reason']) for r in shown] == [
        ('Ana-40', 'Tighten intro'), ('Ana-25', None), ('Bo-12', None), ('Ana-0', 'Start')]


def test_grouping_measures_each_gap_from_the_previous_save():
    gap = GROUP_GAP.total_seconds() / 60
    rows = [row('Ana', 2 * gap - 1), row('Ana', gap - 0.5), row('Ana', 0)]
    assert [r['id'] for r in grouped(rows)] == [rows[0]['id']]
    rows = [row('Ana', gap + 1), row('Ana', 0)]
    assert [r['id'] for r in grouped(rows)] == [rows[0]['id'], rows[1]['id']]


def test_grouped_list_of_the_capped_history_stays_under_a_second(tmp_path):
    repo = tmp_path / 'workspace'
    repo.mkdir()
    subprocess.run(['git', 'init', '-q', str(repo)], check=True)
    count, lines = 600, []
    for index in range(count):
        body = f'version {index}\n'.encode()
        author = 'Ana' if index % 3 else 'Bo'
        message = f'save {index}\n\nReason: change {index}\nScope: workspace\n'.encode() if index % 7 == 0 else f'save {index}\n'.encode()
        lines += [b'commit refs/heads/main', f'author {author} <{author}@x.test> {1_700_000_000 + index * 60} +0000'.encode(),
                  f'committer {author} <{author}@x.test> {1_700_000_000 + index * 60} +0000'.encode(),
                  f'data {len(message)}'.encode(), message]
        lines += [b'M 100644 inline sample.md', f'data {len(body)}'.encode(), body]
    subprocess.run(['git', '-C', str(repo), 'fast-import', '--quiet'], input=b'\n'.join(lines) + b'\n', check=True)
    subprocess.run(['git', '-C', str(repo), 'checkout', '-q', 'main'], check=True)
    started = time.monotonic()
    rows, truncated = list_versions(repo, 'sample.md')
    elapsed = time.monotonic() - started
    assert truncated is True
    assert 0 < len(rows) < 500
    assert elapsed < 1.0, elapsed


async def live_lane(path: Path, tmp_path: Path):
    """A live lane on ``path`` whose writer is the server's own."""
    from doceditor.document_save import write_live_text
    from doceditor.live_document import LiveDocument
    from doceditor.live_lane import DocumentLane

    live = await LiveDocument.open(path, tmp_path / 'live-state.sqlite')

    async def valid(_text, _actor):
        return None

    async def writer(text, prior_hash, actor=None, reason=None):
        await asyncio.to_thread(write_live_text, path, text, prior_hash, actor, reason)

    return live, DocumentLane(live, validate_actor=valid, write=writer)


@pytest.mark.asyncio
async def test_live_lane_writes_are_recorded_by_their_actor(history_scope, tmp_path):
    from doceditor.live_operation import Operation
    from doceditor.live_publication_record import WriteActor

    workspace, thread_id = history_scope
    path = placed(workspace / 'notes' / 'plan.md')
    live, lane = await live_lane(path, tmp_path)
    try:
        actor = WriteActor(thread_id, None)
        (await lane.submit(Operation('replace', content='Live text.\n', actor=actor, reason='Live reason'))).require_landed()
    finally:
        live.close()
    assert path.read_text() == 'Live text.\n'
    assert _git(['log', '-1', '--format=%ae'], cwd=workspace).strip() == f'{thread_id}@agents.corsia.local'
    assert 'Reason: Live reason' in _git(['log', '-1', '--format=%B'], cwd=workspace)


@pytest.mark.asyncio
async def test_live_write_with_a_history_fault_still_lands(history_scope, tmp_path, monkeypatch):
    from doceditor.live_operation import Operation
    from doceditor.live_publication_record import WriteActor
    from infra import guidance_git

    workspace, thread_id = history_scope
    path = placed(workspace / 'notes' / 'plan.md')
    monkeypatch.setattr(guidance_git, '_commit', lambda *_a, **_k: (_ for _ in ()).throw(guidance_git.GitFault('disk full')))
    live, lane = await live_lane(path, tmp_path)
    try:
        result = await lane.submit(Operation('replace', content='Still saved.\n', actor=WriteActor(thread_id, None)))
        result.require_landed()
    finally:
        live.close()
    assert path.read_text() == 'Still saved.\n'


@pytest.mark.asyncio
async def test_restore_of_an_open_document_goes_through_its_lane(history_scope, tmp_path, monkeypatch):
    from api import files
    from doceditor import live_tools

    workspace, thread_id = history_scope
    user_id = owner(thread_id)
    monkeypatch.setattr(files, 'authenticate', lambda _token: {'id': user_id})
    path = placed(workspace / 'notes' / 'plan.md', 'First.\n')
    await save_document_text(DocumentSave(path=str(path), text='First version.\n', thread_id=thread_id), user_id)
    first = _git(['rev-parse', 'HEAD'], cwd=workspace).strip()
    await save_document_text(DocumentSave(path=str(path), text='Second version.\n', thread_id=thread_id), user_id)
    live, lane = await live_lane(path, tmp_path)
    monkeypatch.setattr(live_tools, 'find_live', lambda resolved: SimpleNamespace(lane=lane, artifact_id='a') if resolved == path.resolve() else None)

    async def restore(base: str):
        return await files.restore_version('test', files.RestoreBody(
            path=str(path), version=first, base_sha256=base, thread_id=thread_id))

    try:
        stale = await restore(sha('Not what is open.\n'))
        assert (stale.status_code, json.loads(stale.body)['kind']) == (409, 'stale')
        assert path.read_text() == 'Second version.\n'
        before = int(_git(['rev-list', '--count', 'HEAD'], cwd=workspace))
        result = await restore(sha('Second version.\n'))
    finally:
        live.close()
    assert result == {'version': None, 'sha256': sha('First version.\n')}
    assert path.read_text() == 'First version.\n'
    assert int(_git(['rev-list', '--count', 'HEAD'], cwd=workspace)) == before + 1
    assert _git(['log', '-1', '--format=%ae'], cwd=workspace).strip() == f'{user_id}@users.corsia.local'
    assert 'Reason: Restored ' in _git(['log', '-1', '--format=%B'], cwd=workspace)
