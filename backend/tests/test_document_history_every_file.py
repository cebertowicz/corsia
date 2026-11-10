"""Every kept text file has history: which saves are recorded as document versions, what a document's save
skips, how the list groups saves, and that history never fails a save (live lane included)."""
import asyncio
import hashlib
import json
import subprocess
import threading
import time
from pathlib import Path
from types import SimpleNamespace

import pytest

from doceditor.document_save import DocumentSave, save_document_text
from infra.database import get_db
from infra.document_history import GROUP_GAP, grouped, list_versions
from infra.guidance_targets import history_target
from tests.test_guidance_history import history_scope as history_scope, SKILL_BODY, _IDENTITY, _git


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


def head(workspace: Path) -> str:
    return _git(['rev-parse', 'HEAD'], cwd=workspace).strip()


def register(thread_id: str, path: Path) -> None:
    from registry import service as registry_service

    db = get_db()
    try:
        registry_service.register_write_strict(thread_id, str(path), db=db)
    finally:
        db.close()


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
    tray_working = placed(tray / 'working' / 'draft.md')
    db = get_db()
    try:
        registry_service.register_write_strict(thread_id, str(tray_working), db=db)
    finally:
        db.close()
    top_working = placed(workspace / 'working' / 'draft.md')
    own_working = placed(workspace / 'notes' / 'working' / 'plan.md')
    instruction = placed(tray / 'skills' / 'demo' / 'SKILL.md', SKILL_BODY)
    ignored = placed(workspace / 'private' / 'secret.md')
    (workspace / '.gitignore').write_text('/private/\n')
    nested = placed(workspace / 'projects' / 'demo' / 'source' / 'repo' / 'README.md')
    (nested.parent / '.git').mkdir()

    assert (history_target(document, workspace).document, history_target(document, workspace).registered) == (True, False)
    assert (history_target(registered, workspace).document, history_target(registered, workspace).registered) == (True, True)
    # Only the ephemeral working folders are left out: a tray's own and the workspace's top-level one.
    assert history_target(tray_working, workspace) is None
    assert history_target(top_working, workspace) is None
    assert history_target(own_working, workspace).document is True
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
    before = head(workspace)
    saved = await save_document_text(DocumentSave(path=str(path), text='Kept out.\n', thread_id=thread_id), owner(thread_id))
    assert path.read_text() == 'Kept out.\n'
    assert saved['version'] is None and 'document_history' not in saved
    assert head(workspace) == before


@pytest.mark.asyncio
async def test_a_registered_thread_file_git_ignores_is_recorded_and_an_unregistered_one_is_not(history_scope):
    workspace, thread_id = history_scope
    # The workspace's own rule: thread folders are ignored, except instruction folders.
    (workspace / '.gitignore').write_text('/.threads/*/*\n!/.threads/*/skills/\n')
    tray = workspace / '.threads' / thread_id
    kept = placed(tray / 'reports' / 'summary.md', 'Draft.\n')
    register(thread_id, kept)
    loose = placed(tray / 'scratch' / 'notes.md', 'Scratch.\n')
    assert _git(['check-ignore', '--', str(kept.relative_to(workspace))], cwd=workspace).strip()
    before = head(workspace)
    user_id = owner(thread_id)
    await save_document_text(DocumentSave(path=str(kept), text='Final.\n', thread_id=thread_id), user_id)
    await save_document_text(DocumentSave(path=str(loose), text='More scratch.\n'), user_id)
    assert loose.read_text() == 'More scratch.\n'
    rel = f'.threads/{thread_id}/reports/summary.md'
    assert subjects(workspace, kept) == [f'document save: {rel}', f'document sweep: {rel}']
    assert _git(['show', f'HEAD:{rel}'], cwd=workspace) == 'Final.\n'
    assert subjects(workspace, loose) == []
    assert _git(['rev-list', '--count', f'{before}..HEAD'], cwd=workspace).strip() == '2'
    rows, _truncated = list_versions(workspace, rel)
    assert len(rows) == 2


@pytest.mark.asyncio
async def test_a_registered_thread_file_is_kept_only_when_gits_deciding_rule_is_the_tray_line(history_scope):
    from infra.document_history import history_offered
    from infra.guidance_targets import history_target as target_of

    workspace, thread_id = history_scope
    # The tray-wide line is the one rule registration overrides; a rule Git names instead still holds.
    (workspace / '.gitignore').write_text('/.threads/*/*\n!/.threads/*/skills/\n/.threads/*/private/\n*.secret.md\n')
    (workspace / '.git' / 'info').mkdir(exist_ok=True)
    (workspace / '.git' / 'info' / 'exclude').write_text('*.local.md\n')
    tray = workspace / '.threads' / thread_id
    kept = placed(tray / 'reports' / 'summary.md', 'Draft.\n')
    by_root = placed(tray / 'keys.secret.md', 'token\n')
    by_folder = placed(tray / 'private' / 'plan.md', 'Private.\n')
    by_nested = placed(tray / 'notes.md', 'Local.\n')
    (tray / '.gitignore').write_text('notes.md\n')
    # Git names the tray line for a file in a tray's sub-folder, and the root .gitignore over
    # info/exclude, so these rules do not keep the file out.
    masked = [placed(tray / 'reports' / 'old.secret.md', 'Old.\n'), placed(tray / 'mine.local.md', 'Mine.\n')]
    held = (by_root, by_folder, by_nested)
    for path in (kept, *held, *masked):
        register(thread_id, path)
    user_id = owner(thread_id)
    before = head(workspace)
    for path in (kept, *held, *masked):
        await save_document_text(DocumentSave(path=str(path), text='Changed.\n', thread_id=thread_id), user_id)
        assert path.read_text() == 'Changed.\n'
    changed = set(_git(['diff', '--name-only', before, 'HEAD'], cwd=workspace).split())
    assert changed == {path.relative_to(workspace).as_posix() for path in (kept, *masked)}
    offered = {path.name: history_offered(workspace, (path, target_of(path, workspace))) for path in (kept, *held)}
    assert offered == {'summary.md': True, 'keys.secret.md': False, 'plan.md': False, 'notes.md': False}


@pytest.mark.asyncio
async def test_history_is_offered_only_once_a_version_is_recorded(history_scope):
    from infra.document_history import history_offered
    from infra.guidance_targets import history_target as target_of

    workspace, thread_id = history_scope
    path = placed(workspace / '.threads' / thread_id / 'notes' / 'fresh.md', 'Draft.\n')
    register(thread_id, path)

    def offered() -> bool:
        return history_offered(workspace, (path, target_of(path, workspace)))

    # Registered but never saved: nothing to list, so no History control and no empty panel (VH7).
    assert offered() is False
    await save_document_text(DocumentSave(path=str(path), text='Saved.\n', thread_id=thread_id), owner(thread_id))
    assert offered() is True


@pytest.mark.asyncio
async def test_a_fault_offers_history_so_its_panel_shows_it(history_scope, monkeypatch):
    from fastapi import HTTPException

    from api import files, files_history
    from core import document_history_paths
    from infra import document_history
    from infra.guidance_states import GitFault

    workspace, thread_id = history_scope
    user_id = owner(thread_id)
    monkeypatch.setattr(files, 'authenticate', lambda _token: {'id': user_id})
    path = placed(workspace / 'notes' / 'plan.md')
    real = document_history._git_bytes

    def unreadable_head(repo, *args, **kwargs):
        if args[0] == 'rev-parse' and args[-1].startswith('HEAD:'):
            raise GitFault('fatal: unable to read tree')
        return real(repo, *args, **kwargs)
    # Git failing the open's one lookup offers History rather than hiding it.
    monkeypatch.setattr(document_history, '_git_bytes', unreadable_head)
    assert (await files.content('test', str(path), thread_id=thread_id))['history'] is True
    monkeypatch.setattr(document_history, '_git_bytes', real)
    # A document whose classification fails carries the fault, as a guidance file's does.
    monkeypatch.setattr(document_history_paths, 'is_history_document', lambda *_a, **_k: (_ for _ in ()).throw(OSError('database is locked')))
    target = history_target(path, workspace)
    assert target.document and 'database is locked' in target.fault
    assert (await files.content('test', str(path), thread_id=thread_id))['history'] is True
    with pytest.raises(HTTPException) as failure:
        files_history.history(str(path), user_id, None, thread_id)
    assert failure.value.status_code == 503
    # Its save still lands and says history may be missing.
    saved = await save_document_text(DocumentSave(path=str(path), text='Saved.\n', thread_id=thread_id), user_id)
    assert path.read_text() == 'Saved.\n'
    assert saved['document_history'].startswith('Saved. History may be missing: could not check')


@pytest.mark.asyncio
async def test_a_save_answers_once_its_version_is_recorded_in_turn(history_scope, monkeypatch):
    from doceditor.document_save import DocumentSaveError
    from infra import guidance_git

    workspace, thread_id = history_scope
    user_id = owner(thread_id)
    path = placed(workspace / 'notes' / 'plan.md', 'Start.\n')
    saved = await save_document_text(DocumentSave(path=str(path), text='One.\n', thread_id=thread_id), user_id)
    # The save answers with the version it recorded.
    assert saved['version'] == head(workspace)
    real, release, started = guidance_git._commit, threading.Event(), threading.Event()

    def held(*args, **kwargs):
        started.set()
        assert release.wait(10)
        return real(*args, **kwargs)
    monkeypatch.setattr(guidance_git, '_commit', held)
    save = asyncio.create_task(save_document_text(DocumentSave(path=str(path), text='Two.\n', thread_id=thread_id), user_id))
    assert await asyncio.to_thread(started.wait, 10)
    restore = asyncio.create_task(save_document_text(
        DocumentSave(path=str(path), text='One.\n', thread_id=thread_id), user_id, reason='Restored', history_required=True))
    await asyncio.sleep(0.3)
    # Neither answers while the first holds the history lock; the restore waits its turn behind it.
    assert not save.done() and not restore.done()
    release.set()
    await save
    await restore
    assert _git(['log', '-2', '--format=%s %B'], cwd=workspace).count('Reason: Restored') == 1
    assert _git(['show', 'HEAD:notes/plan.md'], cwd=workspace) == 'One.\n'
    assert _git(['show', 'HEAD~1:notes/plan.md'], cwd=workspace) == 'Two.\n'
    # A restore history cannot record is refused, the file is put back, and its owner hears of no save.
    from doceditor import document_save
    reported = []
    monkeypatch.setattr(document_save, 'report_plain_write', lambda *_a, **_k: reported.append(1))
    monkeypatch.setattr(guidance_git, '_commit', lambda *_a, **_k: (_ for _ in ()).throw(guidance_git.GitFault('disk full')))
    with pytest.raises(DocumentSaveError):
        await save_document_text(DocumentSave(path=str(path), text='Two.\n', thread_id=thread_id), user_id,
                                 reason='Restored', history_required=True)
    assert path.read_text() == 'One.\n'
    assert reported == []


def test_a_document_delete_goes_through_the_same_writer(history_scope):
    from infra.guidance_git import save_with_history
    from infra.guidance_history_paths import digest
    from infra.guidance_messages import saver_author

    workspace, thread_id = history_scope
    path = placed(workspace / 'notes' / 'gone.md', 'Bye.\n')
    target = history_target(path, workspace)
    saved = save_with_history({}, workspace=workspace, target=target, author=saver_author(thread_id=thread_id),
                          deletes={path: digest(b'Bye.\n')})
    assert saved.outcome == 'written' and not path.exists()
    # The text it held (never recorded) is kept first, then the delete.
    assert subjects(workspace, path) == ['document save: notes/gone.md', 'document sweep: notes/gone.md']


@pytest.mark.asyncio
async def test_document_save_skips_the_lesson_check_and_carry_in(history_scope, monkeypatch):
    from core import carry_in_facts
    from infra import lesson_save_gate
    from registry import service as registry_service

    workspace, thread_id = history_scope
    # An agent writes in its own tray; registered, the file is a document.
    path = placed(workspace / '.threads' / thread_id / 'reports' / 'plan.md')
    register(thread_id, path)
    asked: list[str] = []
    monkeypatch.setattr(lesson_save_gate, 'lesson_changes', lambda *_a, **_k: asked.append('lesson') or ())
    monkeypatch.setattr(carry_in_facts, 'record_committed_save', lambda *_a, **_k: asked.append('carry-in'))
    saved = await save_document_text(
        DocumentSave(path=str(path), text='Agent text.\n', thread_id=thread_id), owner(thread_id), by_agent=True)
    assert path.read_text() == 'Agent text.\n'
    # The save answers with its recorded version and no history line.
    assert saved['version'] == head(workspace) and 'document_history' not in saved
    # The text it held before (never recorded) is kept first as its own version, then the save.
    assert subjects(workspace, path) == [
        f'document save: .threads/{thread_id}/reports/plan.md', f'document sweep: .threads/{thread_id}/reports/plan.md']
    assert asked == []
    author = _git(['log', '-1', '--format=%ae'], cwd=workspace).strip()
    assert author == f'{thread_id}@agents.corsia.local'


@pytest.mark.asyncio
async def test_a_save_history_could_not_record_is_reported_and_kept_by_the_next(history_scope, monkeypatch):
    from infra import guidance_git
    from infra.guidance_messages import SAVE_FAILED

    workspace, thread_id = history_scope
    user_id = owner(thread_id)
    path = placed(workspace / 'notes' / 'plan.md')
    await save_document_text(DocumentSave(path=str(path), text='Start.\n', thread_id=thread_id), user_id)
    before = head(workspace)
    real = guidance_git._commit
    monkeypatch.setattr(guidance_git, '_commit', lambda *_a, **_k: (_ for _ in ()).throw(guidance_git.GitFault('disk full')))
    saved = await save_document_text(DocumentSave(path=str(path), text='Kept.\n', thread_id=thread_id), user_id)
    # The save never fails on history: its bytes are on disk, and its answer says the version was not recorded.
    assert path.read_text() == 'Kept.\n'
    assert saved['version'] is None
    assert saved['document_history'] == SAVE_FAILED.format(error='disk full')
    assert head(workspace) == before
    monkeypatch.setattr(guidance_git, '_commit', real)
    saved = await save_document_text(DocumentSave(path=str(path), text='Next.\n', thread_id=thread_id), user_id)
    # The next save keeps the unrecorded text first, by no one's name, then its own under its author.
    assert 'document_history' not in saved and saved['version'] == head(workspace)
    assert _git(['log', '--format=%s', f'{before}..HEAD'], cwd=workspace).splitlines() == [
        'document save: notes/plan.md', 'document sweep: notes/plan.md']
    assert _git(['show', 'HEAD~1:notes/plan.md'], cwd=workspace) == 'Kept.\n'
    assert 'Reason: unrecorded' in _git(['log', '-1', '--format=%B', 'HEAD~1'], cwd=workspace)
    assert _git(['show', 'HEAD:notes/plan.md'], cwd=workspace) == 'Next.\n'
    assert _git(['log', '-1', '--format=%ae'], cwd=workspace).strip() == f'{user_id}@users.corsia.local'


@pytest.mark.asyncio
async def test_a_save_past_a_busy_history_lock_is_written_and_kept_by_the_next(history_scope, monkeypatch):
    from infra import guidance_git

    workspace, thread_id = history_scope
    user_id = owner(thread_id)
    path = placed(workspace / 'notes' / 'plan.md')
    await save_document_text(DocumentSave(path=str(path), text='Start.\n', thread_id=thread_id), user_id)
    before = head(workspace)
    real = guidance_git._acquire
    monkeypatch.setattr(guidance_git, '_acquire', lambda *_a, **_k: (_ for _ in ()).throw(guidance_git._LockBusy('the guidance lock stayed busy')))
    saved = await save_document_text(DocumentSave(path=str(path), text='Waited.\n', thread_id=thread_id), user_id)
    # Written without its version, and the answer says so; nothing is held in memory to retry.
    assert path.read_text() == 'Waited.\n'
    assert saved['version'] is None and 'lock stayed busy' in saved['document_history']
    assert head(workspace) == before
    monkeypatch.setattr(guidance_git, '_acquire', real)
    await save_document_text(DocumentSave(path=str(path), text='Next.\n', thread_id=thread_id), user_id)
    # The next locked save keeps it as an outside version, which carries no author.
    assert _git(['show', 'HEAD~1:notes/plan.md'], cwd=workspace) == 'Waited.\n'
    assert 'Reason: unrecorded' in _git(['log', '-1', '--format=%B', 'HEAD~1'], cwd=workspace)


@pytest.mark.asyncio
async def test_a_save_held_in_its_owner_report_never_holds_another_documents_save(history_scope, monkeypatch):
    from doceditor import document_save

    workspace, thread_id = history_scope
    user_id = owner(thread_id)
    shared = placed(workspace / 'docs' / 'big.md', 'x\n' * 500_000)
    other = placed(workspace / 'notes' / 'other.md', 'Other.\n')
    real, release, reporting = document_save.report_plain_write, threading.Event(), threading.Event()

    def slow_report(decision, resolved, *args, **kwargs):
        # Stands in for the owner report's diff of a 1 MB shared document.
        if resolved == shared.resolve():
            reporting.set()
            assert release.wait(20)
        return real(decision, resolved, *args, **kwargs)
    monkeypatch.setattr(document_save, 'report_plain_write', slow_report)
    big = asyncio.create_task(save_document_text(
        DocumentSave(path=str(shared), text='y\n' * 500_000, thread_id=thread_id), user_id))
    assert await asyncio.to_thread(reporting.wait, 10)
    begun = time.monotonic()
    # The report runs after the big save released the history lock, so this save records at once.
    await save_document_text(DocumentSave(path=str(other), text='Other, edited.\n', thread_id=thread_id), user_id)
    recorded_in = time.monotonic() - begun
    try:
        assert not big.done()
        assert _git(['show', 'HEAD:notes/other.md'], cwd=workspace) == 'Other, edited.\n'
        assert recorded_in < 3, recorded_in
    finally:
        release.set()
        await big
    assert _git(['show', 'HEAD:docs/big.md'], cwd=workspace) == 'y\n' * 500_000


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


def test_grouping_measures_from_each_versions_first_save_so_a_long_session_keeps_versions():
    gap = GROUP_GAP.total_seconds() / 60
    # Saves every few minutes for twice the gap: the window never slides, so the session shows two.
    rows = [row('Ana', 2 * gap - 1), row('Ana', gap + 0.5), row('Ana', gap - 0.5), row('Ana', 0)]
    assert [r['id'] for r in grouped(rows)] == [rows[0]['id'], rows[2]['id']]
    rows = [row('Ana', gap + 1), row('Ana', 0)]
    assert [r['id'] for r in grouped(rows)] == [rows[0]['id'], rows[1]['id']]


@pytest.mark.asyncio
async def test_a_credential_named_file_is_never_recorded_whatever_the_ignore_rules(history_scope):
    from infra.document_history import history_offered
    from infra.guidance_targets import history_target as target_of

    workspace, thread_id = history_scope
    # A workspace made before the scaffold's credential lines: only the tray line, which a registered
    # file in a tray's sub-folder passes.
    (workspace / '.gitignore').write_text('/.threads/*/*\n')
    tray = workspace / '.threads' / thread_id
    secrets = [placed(workspace / '.env', 'TOKEN=x\n'), placed(workspace / 'config' / 'id_rsa', 'key\n'),
               placed(tray / 'reports' / 'deploy.key', 'key\n')]
    register(thread_id, secrets[-1])
    before = head(workspace)
    for path in secrets:
        await save_document_text(DocumentSave(path=str(path), text='Changed.\n', thread_id=thread_id), owner(thread_id))
        assert path.read_text() == 'Changed.\n'
        assert not history_offered(workspace, (path, target_of(path, workspace)))
    assert head(workspace) == before


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
    from doceditor.live_text_write import write_live_text
    from doceditor.live_document import LiveDocument
    from doceditor.live_lane import DocumentLane

    live = await LiveDocument.open(path, tmp_path / 'live-state.sqlite')

    async def valid(_text, _actor):
        return None

    async def writer(text, prior_hash, operation=None):
        await asyncio.to_thread(write_live_text, path, text, prior_hash, operation)

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
@pytest.mark.parametrize('open_live', [False, True], ids=['closed', 'open'])
async def test_agent_file_tool_saves_are_recorded_under_the_agents_name(history_scope, tmp_path, monkeypatch, open_live):
    from doceditor import live_runtime
    from doceditor.live_runtime import LiveDocuments
    from tools.filesystem_edit import EditFileTool, MultiEditTool
    from tools.filesystem_rw import ReadFileTool, WriteFileTool

    workspace, thread_id = history_scope
    # Agents write only in their own tray (or approved shared folders); registered, the file is a document.
    path = placed(workspace / '.threads' / thread_id / 'reports' / 'plan.md', 'One.\n')
    _git(['add', str(path.relative_to(workspace))], cwd=workspace)
    _git([*_IDENTITY, 'commit', '-qm', 'seed'], cwd=workspace)
    register(thread_id, path)
    context = {'_thread_id': thread_id}
    manager = None
    lane_writes: list[str] = []
    if open_live:
        db = get_db()
        try:
            artifact_id = db.execute('SELECT id FROM artifact_identities WHERE path=?', (str(path),)).fetchone()[0]
            db_path = Path(db.execute('PRAGMA database_list').fetchone()[2])
        finally:
            db.close()
        manager = LiveDocuments(tmp_path / 'live', workspace=workspace, db_path=db_path)
        monkeypatch.setattr(live_runtime, '_manager', manager)
        real = live_runtime.write_live_text
        monkeypatch.setattr(live_runtime, 'write_live_text', lambda *a: lane_writes.append(a[1]) or real(*a))
        assert await manager.open(artifact_id, path) is not None
    try:
        await ReadFileTool(workspace=workspace).execute(str(path), **context)
        calls = [
            (WriteFileTool, {'content': 'Two.\n'}),
            (EditFileTool, {'old_string': 'Two.', 'new_string': 'Three.'}),
            (MultiEditTool, {'edits': [{'old_string': 'Three.', 'new_string': 'Four.'}]}),
        ]
        for tool, args in calls:
            before = head(workspace)
            result = await tool(workspace=workspace).execute(path=str(path), **args, **context)
            assert not result.startswith('Error'), result
            # Each save is its own version, authored by the agent that made it, never "Unknown editor".
            assert _git(['log', '--format=%an <%ae>', f'{before}..HEAD'], cwd=workspace).split('\n')[:-1] == [
                f'History Tester <{thread_id}@agents.corsia.local>'], tool.__name__
    finally:
        if manager is not None:
            await manager.close()
    assert path.read_text() == 'Four.\n'
    # An open document's saves went through its live lane, not the closed-file path.
    assert lane_writes == (['Two.\n', 'Three.\n', 'Four.\n'] if open_live else [])


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
async def test_an_outside_edit_behind_a_refused_restore_is_kept_by_the_next_save(history_scope, monkeypatch):
    from api import files

    workspace, thread_id = history_scope
    user_id = owner(thread_id)
    monkeypatch.setattr(files, 'authenticate', lambda _token: {'id': user_id})
    path = placed(workspace / 'notes' / 'plan.md', 'First.\n')
    await save_document_text(DocumentSave(path=str(path), text='First version.\n', thread_id=thread_id), user_id)
    first = head(workspace)
    await save_document_text(DocumentSave(path=str(path), text='Second version.\n', thread_id=thread_id), user_id)
    path.write_text('Outside edit.\n')
    before = int(_git(['rev-list', '--count', 'HEAD'], cwd=workspace))
    refused = await files.restore_version('test', files.RestoreBody(
        path=str(path), version=first, base_sha256=sha('Second version.\n'), thread_id=thread_id))
    assert (refused.status_code, json.loads(refused.body)['kind']) == (409, 'stale')
    # The refusal writes and records nothing.
    assert path.read_text() == 'Outside edit.\n'
    assert int(_git(['rev-list', '--count', 'HEAD'], cwd=workspace)) == before
    # The next save keeps the outside edit first, by no one's name, then its own text.
    await save_document_text(DocumentSave(path=str(path), text='Outside edit.\nTyped.\n', thread_id=thread_id), user_id)
    assert int(_git(['rev-list', '--count', 'HEAD'], cwd=workspace)) == before + 2
    assert 'Reason: unrecorded' in _git(['log', '-1', '--format=%B', 'HEAD~1'], cwd=workspace)
    assert _git(['show', 'HEAD~1:notes/plan.md'], cwd=workspace) == 'Outside edit.\n'


@pytest.mark.asyncio
async def test_a_save_of_the_recorded_text_over_an_outside_edit_keeps_the_edit_first(history_scope):
    workspace, thread_id = history_scope
    user_id = owner(thread_id)
    path = placed(workspace / 'notes' / 'plan.md')
    await save_document_text(DocumentSave(path=str(path), text='Recorded.\n', thread_id=thread_id), user_id)
    before = head(workspace)
    path.write_text('Outside edit.\n')
    # Typed back to exactly what history holds: the outside text is still kept as its own version.
    saved = await save_document_text(DocumentSave(path=str(path), text='Recorded.\n', thread_id=thread_id), user_id)
    assert path.read_text() == 'Recorded.\n'
    assert _git(['log', '--format=%s', f'{before}..HEAD'], cwd=workspace).splitlines() == [
        'document save: notes/plan.md', 'document sweep: notes/plan.md']
    assert _git(['show', 'HEAD~1:notes/plan.md'], cwd=workspace) == 'Outside edit.\n'
    assert 'Reason: unrecorded' in _git(['log', '-1', '--format=%B', 'HEAD~1'], cwd=workspace)
    assert saved['version'] == head(workspace) and _git(['show', 'HEAD:notes/plan.md'], cwd=workspace) == 'Recorded.\n'


@pytest.mark.asyncio
async def test_restoring_the_newest_version_over_an_outside_edit_keeps_the_edit(history_scope, monkeypatch):
    from api import files

    workspace, thread_id = history_scope
    user_id = owner(thread_id)
    monkeypatch.setattr(files, 'authenticate', lambda _token: {'id': user_id})
    path = placed(workspace / 'notes' / 'plan.md')
    await save_document_text(DocumentSave(path=str(path), text='Newest version.\n', thread_id=thread_id), user_id)
    newest = head(workspace)
    path.write_text('Outside edit.\n')
    result = await files.restore_version('test', files.RestoreBody(
        path=str(path), version=newest, base_sha256=sha('Outside edit.\n'), thread_id=thread_id))
    assert result['sha256'] == sha('Newest version.\n') and path.read_text() == 'Newest version.\n'
    # The restore writes history's own newest text, and the outside text it replaced stays recoverable.
    assert _git(['show', 'HEAD~1:notes/plan.md'], cwd=workspace) == 'Outside edit.\n'
    assert 'Reason: unrecorded' in _git(['log', '-1', '--format=%B', 'HEAD~1'], cwd=workspace)
    assert 'Reason: Restored ' in _git(['log', '-1', '--format=%B'], cwd=workspace)
    assert _git(['show', 'HEAD:notes/plan.md'], cwd=workspace) == 'Newest version.\n'


@pytest.mark.asyncio
async def test_restore_of_an_open_document_goes_through_its_lane(history_scope, tmp_path, monkeypatch):
    from api import files
    from doceditor import live_tools

    workspace, thread_id = history_scope
    user_id = owner(thread_id)
    monkeypatch.setattr(files, 'authenticate', lambda _token: {'id': user_id})
    path = placed(workspace / 'notes' / 'plan.md', 'First.\n')
    await save_document_text(DocumentSave(path=str(path), text='First version.\n', thread_id=thread_id), user_id)
    first = head(workspace)
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
        # An edit made outside CORSIA after the person opened the version is never replaced; the refusal
        # writes nothing, and the edit is kept by the next save of the document.
        path.write_text('Second version.\nOutside edit.\n')
        before = int(_git(['rev-list', '--count', 'HEAD'], cwd=workspace))
        outside = await restore(sha('Second version.\n'))
        assert (outside.status_code, json.loads(outside.body)['kind']) == (409, 'stale')
        assert path.read_text() == 'Second version.\nOutside edit.\n'
        assert live.text == 'Second version.\nOutside edit.\n'
        assert int(_git(['rev-list', '--count', 'HEAD'], cwd=workspace)) == before
        # Restored from the text the person now sees, it lands.
        result = await restore(sha('Second version.\nOutside edit.\n'))
    finally:
        live.close()
    assert result == {'version': None, 'sha256': sha('First version.\n')}
    assert path.read_text() == 'First version.\n'
    # The answer comes only once the version is recorded, so a history reloaded on it shows the restore,
    # after the outside edit it replaced.
    assert int(_git(['rev-list', '--count', 'HEAD'], cwd=workspace)) == before + 2
    assert _git(['show', 'HEAD~1:notes/plan.md'], cwd=workspace) == 'Second version.\nOutside edit.\n'
    assert _git(['log', '-1', '--format=%ae'], cwd=workspace).strip() == f'{user_id}@users.corsia.local'
    assert 'Reason: Restored ' in _git(['log', '-1', '--format=%B'], cwd=workspace)


@pytest.mark.asyncio
async def test_an_outside_edit_caught_at_a_live_restores_rename_is_never_replaced(history_scope, tmp_path, monkeypatch):
    from api import files
    from doceditor import live_lane as lane_module, live_tools

    workspace, thread_id = history_scope
    user_id = owner(thread_id)
    monkeypatch.setattr(files, 'authenticate', lambda _token: {'id': user_id})
    path = placed(workspace / 'notes' / 'plan.md', 'First.\n')
    await save_document_text(DocumentSave(path=str(path), text='First version.\n', thread_id=thread_id), user_id)
    first = head(workspace)
    await save_document_text(DocumentSave(path=str(path), text='Second version.\n', thread_id=thread_id), user_id)
    live, lane = await live_lane(path, tmp_path)
    monkeypatch.setattr(live_tools, 'find_live', lambda resolved: SimpleNamespace(lane=lane, artifact_id='a') if resolved == path.resolve() else None)
    edits = []

    async def outside_edit(stage: str) -> None:
        # The edit lands after the restore's check and before its write's compare-and-swap.
        if stage == 'record' and not edits:
            edits.append(stage)
            path.write_text('Second version.\nOutside edit.\n')
    monkeypatch.setattr(lane_module, '_checkpoint', outside_edit)
    try:
        refused = await files.restore_version('test', files.RestoreBody(
            path=str(path), version=first, base_sha256=sha('Second version.\n'), thread_id=thread_id))
    finally:
        live.close()
    assert edits == ['record']
    assert (refused.status_code, json.loads(refused.body)['kind']) == (409, 'stale')
    assert path.read_text() == 'Second version.\nOutside edit.\n'

