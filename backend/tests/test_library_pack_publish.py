import stat

import pytest

from library.pack_publish import publish_pack


def test_publishes_regular_read_only_files_without_overwrite(tmp_path):
    files = {"manifest.yaml": b"one", "context/PROJECT.md": b"two"}
    dest = publish_pack(tmp_path, "sample", "1.0.0", files)
    assert {p.relative_to(dest).as_posix() for p in dest.rglob("*") if p.is_file()} == set(files)
    assert dest.stat().st_mode & 0o222 == 0
    assert (dest / "context").stat().st_mode & 0o222 == 0
    for path, content in files.items():
        target = dest / path
        assert target.read_bytes() == content
        assert stat.S_ISREG(target.lstat().st_mode)
        assert target.stat().st_mode & 0o222 == 0
    with pytest.raises(FileExistsError):
        publish_pack(tmp_path, "sample", "1.0.0", {"manifest.yaml": b"changed"})
    assert (dest / "manifest.yaml").read_bytes() == b"one"
    assert not list(dest.parent.glob(".pack-*"))


def test_refuses_symlinked_pack_parent(tmp_path):
    outside = tmp_path / "outside"
    outside.mkdir()
    (tmp_path / "packs").symlink_to(outside, target_is_directory=True)
    with pytest.raises(ValueError, match="symlink"):
        publish_pack(tmp_path, "sample", "1.0.0", {"manifest.yaml": b"one"})
    assert not list(outside.iterdir())
