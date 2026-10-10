"""Browser socket storage is private and optional until browser work begins."""

import stat
import tempfile
from pathlib import Path

import pytest

from host_access import browser_identity as identity, browser_sessions as sessions


def test_owner_construction_does_not_create_socket_storage(monkeypatch):
    with tempfile.TemporaryDirectory(prefix='mb-', dir='/tmp') as short_root:
        root = Path(short_root)
        monkeypatch.setattr(identity, 'default_socket_root', lambda: root)
        first = sessions.BrowserSessionOwner(namespace='corsia-test', instance_key='instance')
        second = sessions.BrowserSessionOwner(namespace='corsia-test', instance_key='instance')
        assert list(root.iterdir()) == []
        assert first.socket_dir != second.socket_dir
        assert first.socket_dir.parent == root
        assert not first.socket_dir.exists()
        created = identity.ensure_private_dir(first.socket_dir)
        assert created == first.socket_dir.resolve(strict=True)
        assert stat.S_IMODE(created.stat().st_mode) == 0o700


def test_private_dir_refuses_preexisting_symlink_without_chmod(tmp_path):
    target = tmp_path / 'target'
    target.mkdir(mode=0o755)
    link = tmp_path / 'link'
    link.symlink_to(target, target_is_directory=True)
    before = stat.S_IMODE(target.stat().st_mode)
    with pytest.raises(identity.BrowserIdentityError):
        identity.ensure_private_dir(link)
    assert stat.S_IMODE(target.stat().st_mode) == before
