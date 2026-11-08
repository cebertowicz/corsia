"""Real Git coverage for recorded recovery of failed guidance index syncs."""

import json
import os
import signal
import subprocess
import time
from contextlib import contextmanager

from infra.guidance_git import _commit, startup_sweep
from tests.test_guidance_history import history_scope, _fake_lsof, _git, _history, _save
from tests.test_project_repo_lock_recovery import _lock as _index_lock


@contextmanager
def held_index(workspace, monkeypatch, tmp_path):
    lock = _index_lock(workspace / '.git')
    ready = tmp_path / 'holder-ready'
    ready.unlink(missing_ok=True)
    holder = subprocess.Popen(['bash', '-c', 'exec 9> "$1"; touch "$2"; sleep 300', '_', str(lock), str(ready)], start_new_session=True)
    try:
        deadline = time.monotonic() + 3
        while not ready.exists() and holder.poll() is None and time.monotonic() < deadline:
            time.sleep(.02)
        assert ready.exists()
        lsof_dir = tmp_path / f'lsof-{holder.pid}'
        lsof_dir.mkdir()
        _fake_lsof(monkeypatch, lsof_dir, f"printf '%s\\n' {holder.pid}")
        yield lock
    finally:
        os.killpg(holder.pid, signal.SIGTERM)
        holder.wait(timeout=3)
        lock.unlink(missing_ok=True)


def index_blob(workspace, rel):
    return _git(['ls-files', '-s', '--', rel], cwd=workspace).strip()


def seed(workspace):
    path = _history(workspace)
    assert _save(workspace, path, 'zero\n').outcome == 'written'
    return path, path.relative_to(workspace).as_posix()


def failed_saves(workspace, path, monkeypatch, tmp_path, count=1):
    with held_index(workspace, monkeypatch, tmp_path):
        for n in range(count):
            result = _save(workspace, path, f'saved-{n}\n')
            assert result.outcome == 'written' and result.unsynced


def records(workspace):
    path = workspace / '.git' / 'corsia' / 'unsynced-index.json'
    return json.loads(path.read_text()) if path.exists() else []


def test_same_bytes_repairs_and_reports(history_scope, monkeypatch, tmp_path):
    workspace, _ = history_scope
    path, rel = seed(workspace)
    failed_saves(workspace, path, monkeypatch, tmp_path)
    old_blob = index_blob(workspace, rel).split()[1]
    result = _save(workspace, path, path.read_text())
    assert not _git(['diff', '--cached', '--name-only', 'HEAD', '--', rel], cwd=workspace)
    assert rel in result.line and old_blob in result.line and 'repair' in result.line.lower()


def test_startup_reports_old_index_blob(history_scope, monkeypatch, tmp_path):
    from loguru import logger

    workspace, _ = history_scope
    path, rel = seed(workspace)
    failed_saves(workspace, path, monkeypatch, tmp_path)
    old_blob = index_blob(workspace, rel).split()[1]
    messages = []
    sink = logger.add(messages.append)
    try:
        startup_sweep(workspace)
    finally:
        logger.remove(sink)
    assert any(rel in str(message) and old_blob in str(message) for message in messages)
    assert not records(workspace)


def test_failure_records_only_matching_pre_save_entry(history_scope, monkeypatch, tmp_path):
    workspace, _ = history_scope
    path, rel = seed(workspace)
    old = index_blob(workspace, rel).split()[1]
    failed_saves(workspace, path, monkeypatch, tmp_path)
    saved = _git(['rev-parse', f'HEAD:{rel}'], cwd=workspace).strip()
    assert records(workspace) == [{'path': rel, 'from': ['100644', old], 'to': ['100644', saved], 'commit': _git(['rev-parse', 'HEAD'], cwd=workspace).strip()}]


def test_repeated_failed_saves_extend_record_and_recover_latest(history_scope, monkeypatch, tmp_path):
    workspace, _ = history_scope
    path, rel = seed(workspace)
    old = index_blob(workspace, rel).split()[1]
    failed_saves(workspace, path, monkeypatch, tmp_path, count=2)
    latest = _git(['rev-parse', f'HEAD:{rel}'], cwd=workspace).strip()
    pending = records(workspace)
    assert len(pending) == 1
    assert pending[0]['from'] == ['100644', old]
    assert pending[0]['to'] == ['100644', latest]
    _save(workspace, path, path.read_text())
    assert index_blob(workspace, rel).split()[1] == latest
    assert not records(workspace)


def test_failure_with_preexisting_staging_does_not_record(history_scope, monkeypatch, tmp_path):
    workspace, _ = history_scope
    path, rel = seed(workspace)
    path.write_text('staged by person\n')
    _git(['add', '--', rel], cwd=workspace)
    before = index_blob(workspace, rel)
    with held_index(workspace, monkeypatch, tmp_path):
        result = _commit(workspace, {rel: b'new save\n'}, 'save', 'T <t@t>')
        assert result.unsynced == (rel,)
    assert index_blob(workspace, rel) == before
    assert not records(workspace)


def test_staging_after_failure_is_preserved_and_record_dropped(history_scope, monkeypatch, tmp_path):
    workspace, _ = history_scope
    path, rel = seed(workspace)
    failed_saves(workspace, path, monkeypatch, tmp_path)
    path.write_text('person staged\n')
    _git(['add', '--', rel], cwd=workspace)
    staged = index_blob(workspace, rel)
    path.write_text('saved-0\n')
    result = _save(workspace, path, path.read_text())
    assert index_blob(workspace, rel) == staged
    assert not records(workspace)
    assert rel in result.line and 'index entry changed' in result.line


def test_head_moved_drops_record(history_scope, monkeypatch, tmp_path):
    workspace, _ = history_scope
    path, rel = seed(workspace)
    failed_saves(workspace, path, monkeypatch, tmp_path)
    before = index_blob(workspace, rel)
    from infra.guidance_git import _git as guidance_git
    from infra.guidance_index_sync import consume_records
    env = {'GIT_INDEX_FILE': str(tmp_path / 'other-index')}
    guidance_git(workspace, 'read-tree', 'HEAD', env=env)
    blob = guidance_git(workspace, 'hash-object', '-w', '--stdin', data=b'later\n').strip()
    guidance_git(workspace, 'update-index', '--add', '--cacheinfo', f'100644,{blob},{rel}', env=env)
    tree = guidance_git(workspace, 'write-tree', env=env).strip()
    parent = guidance_git(workspace, 'rev-parse', 'HEAD').strip()
    identity = {f'GIT_{role}_{field}': value for role in ('AUTHOR', 'COMMITTER')
                for field, value in (('NAME', 'Outside'), ('EMAIL', 'outside@example.test'))}
    sha = guidance_git(workspace, 'commit-tree', tree, '-p', parent, '-m', 'external', env=identity).strip()
    guidance_git(workspace, 'update-ref', 'HEAD', sha, parent)
    repaired, notes = consume_records(guidance_git, workspace)
    assert not repaired and any('HEAD entry changed' in note for note in notes)
    assert not records(workspace)
    assert index_blob(workspace, rel) == before


def test_live_lock_on_unchanged_save_reports_fault(history_scope, monkeypatch, tmp_path):
    workspace, _ = history_scope
    path, rel = seed(workspace)
    failed_saves(workspace, path, monkeypatch, tmp_path)
    before = index_blob(workspace, rel)
    with held_index(workspace, monkeypatch, tmp_path) as lock:
        result = _save(workspace, path, path.read_text())
        assert result.outcome == 'unchanged' and lock.exists()
        assert rel in result.line and 'preserved' in result.line
        assert index_blob(workspace, rel) == before
        assert len(records(workspace)) == 1


def test_recovery_atomically_preserves_other_staging(history_scope, monkeypatch, tmp_path):
    import infra.guidance_index_sync as sync

    workspace, _ = history_scope
    path, rel = seed(workspace)
    failed_saves(workspace, path, monkeypatch, tmp_path)
    other = workspace / 'other.txt'
    other.write_text('staged independently\n')
    _git(['add', '--', other.name], cwd=workspace)
    staged = index_blob(workspace, other.name)
    index = workspace / '.git' / 'index'
    before = index.read_bytes()
    replace = os.replace
    seen = []

    def observe(src, dst):
        if str(src) == str(index) + '.lock' and str(dst) == str(index):
            assert index.read_bytes() == before
            assert os.path.exists(src)
            seen.append(True)
        return replace(src, dst)

    monkeypatch.setattr(sync.os, 'replace', observe)
    _save(workspace, path, path.read_text())
    assert seen == [True]
    assert index_blob(workspace, other.name) == staged
    assert not _git(['diff', '--cached', '--name-only', 'HEAD', '--', rel], cwd=workspace)
    assert not records(workspace)


def test_missing_entry_is_added(history_scope, monkeypatch, tmp_path):
    workspace, _ = history_scope
    path = _history(workspace)
    rel = path.relative_to(workspace).as_posix()
    failed_saves(workspace, path, monkeypatch, tmp_path)
    assert not index_blob(workspace, rel)
    result = _save(workspace, path, path.read_text())
    assert index_blob(workspace, rel) and rel in result.line and 'absent' in result.line


def test_failed_removal_is_recovered(history_scope, monkeypatch, tmp_path):
    workspace, _ = history_scope
    path, rel = seed(workspace)
    with held_index(workspace, monkeypatch, tmp_path):
        result = _commit(workspace, {rel: None}, 'remove', 'T <t@t>')
        assert result.unsynced == (rel,)
    assert records(workspace)[0]['to'] is None
    path.unlink()
    startup_sweep(workspace)
    assert not index_blob(workspace, rel)
    assert not records(workspace)


def test_unmerged_entry_is_preserved(history_scope):
    workspace, _ = history_scope
    path, rel = seed(workspace)
    blob = _git(['rev-parse', f'HEAD:{rel}'], cwd=workspace).strip()
    _git(['update-index', '--force-remove', '--', rel], cwd=workspace)
    proc = subprocess.run(['git', 'update-index', '--index-info'], cwd=workspace, input=f'100644 {blob} 1\t{rel}\n100644 {blob} 2\t{rel}\n100644 {blob} 3\t{rel}\n', text=True, capture_output=True)
    assert proc.returncode == 0, proc.stderr
    before = index_blob(workspace, rel)
    _commit(workspace, {rel: path.read_bytes()}, 'same', 'T <t@t>')
    assert index_blob(workspace, rel) == before


def test_git_fault_keeps_record_for_next_attempt(history_scope, monkeypatch, tmp_path):
    import infra.guidance_index_sync as sync
    from infra.guidance_states import GitFault

    workspace, _ = history_scope
    path, rel = seed(workspace)
    failed_saves(workspace, path, monkeypatch, tmp_path)
    pending = records(workspace)
    before = index_blob(workspace, rel)
    consume = sync._consume_one

    def fault(*args):
        raise GitFault('git update-index failed: transient')

    monkeypatch.setattr(sync, '_consume_one', fault)
    result = _save(workspace, path, path.read_text())
    assert rel in result.line and 'transient' in result.line
    assert records(workspace) == pending and index_blob(workspace, rel) == before
    monkeypatch.setattr(sync, '_consume_one', consume)
    _save(workspace, path, path.read_text())
    assert not _git(['diff', '--cached', '--name-only', 'HEAD', '--', rel], cwd=workspace)
    assert not records(workspace)


def test_other_paths_are_repaired_but_not_reported(history_scope, monkeypatch, tmp_path):
    workspace, _ = history_scope
    path, rel = seed(workspace)
    failed_saves(workspace, path, monkeypatch, tmp_path)
    old_blob = index_blob(workspace, rel).split()[1]
    result = _commit(workspace, {'unrelated.txt': b'other\n'}, 'other', 'T <t@t>')
    shown = '\n'.join(result.unsynced_line())
    assert rel not in shown and old_blob not in shown
    assert not _git(['diff', '--cached', '--name-only', 'HEAD', '--', rel], cwd=workspace)
    assert not records(workspace)


def legacy_failed_saves(workspace, path, monkeypatch, tmp_path, count):
    """Failed saves as a build without records left them: the stale entry and no record."""
    failed_saves(workspace, path, monkeypatch, tmp_path, count)
    (workspace / '.git' / 'corsia' / 'unsynced-index.json').unlink()


def seed_and_repair(workspace):
    from infra.guidance_git import _git as guidance_git
    from infra.guidance_index_sync import consume_records, seed_legacy_records
    seed_legacy_records(guidance_git, workspace)
    return consume_records(guidance_git, workspace)


def test_startup_repairs_entry_left_before_records(history_scope, monkeypatch, tmp_path):
    workspace, _ = history_scope
    path, rel = seed(workspace)
    legacy_failed_saves(workspace, path, monkeypatch, tmp_path, count=3)
    assert _git(['diff', '--cached', '--name-only', 'HEAD', '--', rel], cwd=workspace).strip() == rel
    startup_sweep(workspace)
    assert not _git(['diff', '--cached', '--name-only', 'HEAD', '--', rel], cwd=workspace)
    assert (workspace / '.git' / 'corsia' / 'legacy-index-seeded').exists()
    assert not records(workspace)


def test_legacy_check_leaves_an_edit_on_disk_alone(history_scope, monkeypatch, tmp_path):
    workspace, _ = history_scope
    path, rel = seed(workspace)
    legacy_failed_saves(workspace, path, monkeypatch, tmp_path, count=1)
    path.write_text('edited after the save\n')
    before = index_blob(workspace, rel)
    repaired, notes = seed_and_repair(workspace)
    assert not repaired and not notes
    assert index_blob(workspace, rel) == before


def test_legacy_check_stops_at_an_outside_commit(history_scope, monkeypatch, tmp_path):
    workspace, _ = history_scope
    path, rel = seed(workspace)
    old = index_blob(workspace, rel)
    path.write_text('outside\n')
    _git(['add', '--', rel], cwd=workspace)
    _git(['-c', 'user.name=Outside', '-c', 'user.email=outside@example.test', 'commit', '-q', '-m', 'outside'], cwd=workspace)
    _git(['update-index', '--cacheinfo', ','.join([old.split()[0], old.split()[1], rel])], cwd=workspace)
    before = index_blob(workspace, rel)
    repaired, notes = seed_and_repair(workspace)
    assert not repaired and not notes
    assert index_blob(workspace, rel) == before


def test_legacy_check_runs_once(history_scope, monkeypatch, tmp_path):
    workspace, _ = history_scope
    path, rel = seed(workspace)
    seed_and_repair(workspace)
    legacy_failed_saves(workspace, path, monkeypatch, tmp_path, count=1)
    before = index_blob(workspace, rel)
    repaired, notes = seed_and_repair(workspace)
    assert not repaired and not notes
    assert index_blob(workspace, rel) == before
