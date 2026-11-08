"""Real Git coverage for recovery of guidance index entries left behind by saves."""

import os
import signal
import subprocess
import time
from contextlib import contextmanager

import pytest

from infra.guidance_git import _commit, _git as guidance_git, startup_sweep
from infra.guidance_index_sync import _previous_blobs, recovery_args
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


@pytest.mark.parametrize('route', ['same', 'new', 'startup'])
def test_three_failed_saves_recover(history_scope, monkeypatch, tmp_path, route):
    workspace, _ = history_scope
    path, rel = seed(workspace)
    failed_saves(workspace, path, monkeypatch, tmp_path, 3)
    if route == 'startup':
        startup_sweep(workspace)
    else:
        _save(workspace, path, path.read_text() if route == 'same' else 'new\n')
    assert not _git(['diff', '--cached', '--name-only', 'HEAD', '--', rel], cwd=workspace)


def test_person_staged_blob_is_preserved(history_scope, monkeypatch, tmp_path):
    workspace, _ = history_scope
    path, rel = seed(workspace)
    failed_saves(workspace, path, monkeypatch, tmp_path)
    path.write_text('person\n')
    _git(['add', '--', rel], cwd=workspace)
    staged = index_blob(workspace, rel)
    path.write_text('saved-0\n')
    result = _save(workspace, path, path.read_text())
    assert index_blob(workspace, rel) == staged
    assert rel in result.line and 'does not match' in result.line


def test_staged_human_parent_blob_matches_recovery_rule(history_scope):
    workspace, _ = history_scope
    path = _history(workspace)
    rel = path.relative_to(workspace).as_posix()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text('human baseline\n')
    _git(['add', '--', rel], cwd=workspace)
    _git(['-c', 'user.name=Person', '-c', 'user.email=person@example.org', 'commit', '-qm', 'human'], cwd=workspace)
    old = index_blob(workspace, rel).split()[1]
    _save(workspace, path, 'corsia save\n')
    _git(['update-index', '--cacheinfo', f'100644,{old},{rel}'], cwd=workspace)
    result = _save(workspace, path, path.read_text())
    assert not _git(['diff', '--cached', '--name-only', 'HEAD', '--', rel], cwd=workspace)
    assert rel in result.line and 'repair' in result.line.lower()


def test_staged_mode_change_is_preserved(history_scope):
    workspace, _ = history_scope
    path, rel = seed(workspace)
    _git(['update-index', '--chmod=+x', '--', rel], cwd=workspace)
    staged = index_blob(workspace, rel)
    result = _commit(workspace, {rel: path.read_bytes()}, 'same', 'T <t@t>')
    assert index_blob(workspace, rel) == staged
    assert 'staged mode preserved' in result.unsynced_line()[0]


def test_disk_different_is_preserved(history_scope, monkeypatch, tmp_path):
    workspace, _ = history_scope
    path, rel = seed(workspace)
    failed_saves(workspace, path, monkeypatch, tmp_path)
    staged = index_blob(workspace, rel)
    path.write_text('outside edit\n')
    _commit(workspace, {rel: b'saved-0\n'}, 'same', 'T <t@t>')
    assert index_blob(workspace, rel) == staged


def test_live_lock_keeps_failed_save_and_reports(history_scope, monkeypatch, tmp_path):
    workspace, _ = history_scope
    path, rel = seed(workspace)
    with held_index(workspace, monkeypatch, tmp_path) as lock:
        result = _save(workspace, path, 'locked\n')
        assert result.outcome == 'written' and rel in result.unsynced
        assert lock.exists() and 'preserved' in result.line


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


def test_person_commit_stops_history_walk(history_scope, monkeypatch, tmp_path):
    workspace, _ = history_scope
    path, rel = seed(workspace)
    old = index_blob(workspace, rel).split()[1]
    path.write_text('person commit\n')
    _git(['add', '--', rel], cwd=workspace)
    _git(['-c', 'user.name=Person', '-c', 'user.email=person@example.org', 'commit', '-qm', 'person'], cwd=workspace)
    _save(workspace, path, 'new\n')
    _git(['update-index', '--cacheinfo', f'100644,{old},{rel}'], cwd=workspace)
    staged = index_blob(workspace, rel)
    _save(workspace, path, path.read_text())
    assert index_blob(workspace, rel) == staged


def test_person_merge_touching_path_stops_history_walk(history_scope):
    workspace, _ = history_scope
    path, rel = seed(workspace)
    old = index_blob(workspace, rel).split()[1]
    _save(workspace, path, 'prior corsia save\n')
    branch = _git(['branch', '--show-current'], cwd=workspace).strip()
    _git(['switch', '-c', 'human-side'], cwd=workspace)
    path.write_text('human side\n')
    _git(['add', '--', rel], cwd=workspace)
    _git(['-c', 'user.name=Person', '-c', 'user.email=person@example.org', 'commit', '-qm', 'human side'], cwd=workspace)
    _git(['switch', branch], cwd=workspace)
    marker = workspace / 'marker'
    marker.write_text('other\n')
    _git(['add', '--', marker.name], cwd=workspace)
    _git(['-c', 'user.name=Person', '-c', 'user.email=person@example.org', 'commit', '-qm', 'other'], cwd=workspace)
    _git(['-c', 'user.name=Person', '-c', 'user.email=person@example.org', 'merge', '--no-ff', '-qm', 'merge', 'human-side'], cwd=workspace)
    _save(workspace, path, 'corsia after merge\n')
    _git(['update-index', '--cacheinfo', f'100644,{old},{rel}'], cwd=workspace)
    before = index_blob(workspace, rel)
    _save(workspace, path, path.read_text())
    assert index_blob(workspace, rel) == before


def test_missing_entry_is_added(history_scope, monkeypatch, tmp_path):
    workspace, _ = history_scope
    path = _history(workspace)
    rel = path.relative_to(workspace).as_posix()
    failed_saves(workspace, path, monkeypatch, tmp_path)
    assert not index_blob(workspace, rel)
    result = _save(workspace, path, path.read_text())
    assert index_blob(workspace, rel) and rel in result.line and 'absent' in result.line


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


def test_save_repairs_only_its_own_guidance_paths(history_scope, monkeypatch, tmp_path):
    workspace, _ = history_scope
    path, rel = seed(workspace)
    other = _history(workspace, 'shell')
    _save(workspace, other, 'before\n')
    failed_saves(workspace, path, monkeypatch, tmp_path)
    before = index_blob(workspace, rel)
    _save(workspace, other, 'after\n')
    assert index_blob(workspace, rel) == before
    startup_sweep(workspace)
    assert not _git(['diff', '--cached', '--name-only', 'HEAD', '--', rel], cwd=workspace)


def test_startup_does_not_touch_non_guidance(history_scope, monkeypatch, tmp_path):
    workspace, _ = history_scope
    path, rel = seed(workspace)
    other = workspace / 'ordinary.txt'
    other.write_text('one\n')
    _git(['add', '--', other.name], cwd=workspace)
    _git(['-c', 'user.name=Person', '-c', 'user.email=person@example.org', 'commit', '-qm', 'other'], cwd=workspace)
    other.write_text('staged\n')
    _git(['add', '--', other.name], cwd=workspace)
    before = index_blob(workspace, other.name)
    failed_saves(workspace, path, monkeypatch, tmp_path)
    startup_sweep(workspace)
    assert index_blob(workspace, other.name) == before
    assert not _git(['diff', '--cached', '--name-only', 'HEAD', '--', rel], cwd=workspace)


def test_history_walk_uses_one_log_for_unmatched_staged_blob(history_scope):
    workspace, _ = history_scope
    path = workspace / 'guidance with space\nand newline.txt'
    rel = path.name
    blobs = []
    for n in range(6):
        path.write_text(f'save {n}\n')
        _git(['add', '--', rel], cwd=workspace)
        _git(['-c', 'user.name=CORSIA', '-c', 'user.email=corsia@localhost', 'commit', '-qm', f'save {n}'], cwd=workspace)
        blobs.append(_git(['rev-parse', f'HEAD:{rel}'], cwd=workspace).strip())
    assert list(_previous_blobs(guidance_git, workspace, rel)) == [('100644', blob) for blob in reversed(blobs[:-1])] + [None]
    path.write_text('person staged\n')
    _git(['add', '--', rel], cwd=workspace)
    path.write_text('save 5\n')
    calls = []

    def recording_git(repo, *args, **kwargs):
        calls.append(args[0])
        return guidance_git(repo, *args, **kwargs)

    result, reason = recovery_args(recording_git, workspace, rel)
    assert result is None and reason == 'index does not match a CORSIA save parent'
    assert calls.count('log') == 1 and 'rev-list' not in calls
    assert calls.count('ls-tree') == 1


def test_corsia_merge_yields_first_parent_entry(history_scope):
    workspace, _ = history_scope
    path = workspace / 'merge path.txt'
    rel = path.name
    path.write_text('baseline\n')
    _git(['add', '--', rel], cwd=workspace)
    _git(['-c', 'user.name=Person', '-c', 'user.email=person@example.org', 'commit', '-qm', 'baseline'], cwd=workspace)
    baseline = _git(['rev-parse', f'HEAD:{rel}'], cwd=workspace).strip()
    branch = _git(['branch', '--show-current'], cwd=workspace).strip()
    _git(['switch', '-c', 'merge-side'], cwd=workspace)
    path.write_text('side\n')
    _git(['add', '--', rel], cwd=workspace)
    _git(['-c', 'user.name=Person', '-c', 'user.email=person@example.org', 'commit', '-qm', 'side'], cwd=workspace)
    _git(['switch', branch], cwd=workspace)
    (workspace / 'merge-marker').write_text('main\n')
    _git(['add', '--', 'merge-marker'], cwd=workspace)
    _git(['-c', 'user.name=Person', '-c', 'user.email=person@example.org', 'commit', '-qm', 'main'], cwd=workspace)
    _git(['-c', 'user.name=CORSIA', '-c', 'user.email=corsia@localhost', 'merge', '--no-ff', '-qm', 'merge', 'merge-side'], cwd=workspace)
    assert list(_previous_blobs(guidance_git, workspace, rel)) == [('100644', baseline)]
