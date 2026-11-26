"""Startup sweep deletes only old, unowned temporary directories."""
import json
import os
import time

from host_access import browser_copy_sweep


def test_sweep_preserves_live_recent_saved_and_symlink(tmp_path, monkeypatch):
    root = tmp_path / "profiles"
    root.mkdir()
    sockets = tmp_path / "sockets"
    owner = sockets / "owner"
    owner.mkdir(parents=True)
    stale = root / "session-stale"
    live = root / "session-live"
    recent = root / "session-recent"
    saved = root / "person"
    for folder in (stale, live, recent, saved):
        folder.mkdir()
    link = root / "session-link"
    link.symlink_to(saved, target_is_directory=True)
    old = time.time() - browser_copy_sweep.MIN_AGE_SECONDS - 10
    for folder in (stale, live, saved):
        os.utime(folder, (old, old))
    record = owner / "example.chrome"
    record.write_text(json.dumps({"pid": 54321, "profile": str(live)}))
    monkeypatch.setattr(browser_copy_sweep.os, "killpg", lambda *_args: None)
    assert browser_copy_sweep.sweep(profile_root=root, socket_root=sockets) == 1
    assert not stale.exists()
    assert live.is_dir() and recent.is_dir() and saved.is_dir() and link.is_symlink()


def test_symlink_profile_root_is_not_followed(tmp_path):
    actual = tmp_path / "actual"
    actual.mkdir()
    stale = actual / "session-stale"
    stale.mkdir()
    old = time.time() - browser_copy_sweep.MIN_AGE_SECONDS - 10
    os.utime(stale, (old, old))
    link = tmp_path / "profile-link"
    link.symlink_to(actual, target_is_directory=True)
    assert browser_copy_sweep.sweep(profile_root=link, socket_root=tmp_path / "sockets") == 0
    assert stale.is_dir()


def test_unreadable_record_holds_sweep(tmp_path):
    root = tmp_path / "profiles"
    root.mkdir()
    stale = root / "session-stale"
    stale.mkdir()
    old = time.time() - browser_copy_sweep.MIN_AGE_SECONDS - 10
    os.utime(stale, (old, old))
    owner = tmp_path / "sockets" / "owner"
    owner.mkdir(parents=True)
    (owner / "bad.chrome").write_text("not-json")
    assert browser_copy_sweep.sweep(profile_root=root, socket_root=owner.parent) == 0
    assert stale.is_dir()
