"""Tests for scripts/check_large_files.py."""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from check_large_files import MAX_BYTES, main  # noqa: E402


def _write(path: Path, size: int) -> str:
    path.write_bytes(b"x" * size)
    return str(path)


def test_oversized_file_refused(tmp_path, capsys):
    big = _write(tmp_path / "dump.json", MAX_BYTES + 1)
    assert main([big], allowlist={}) == 1
    assert "dump.json" in capsys.readouterr().err


def test_file_at_limit_passes(tmp_path):
    ok = _write(tmp_path / "small.json", MAX_BYTES)
    assert main([ok], allowlist={}) == 0


def test_allowlisted_file_passes(tmp_path):
    big = _write(tmp_path / "icon.png", MAX_BYTES + 1)
    assert main([big], allowlist={"*/icon.png": "shipped asset"}) == 0


def test_base_mode_flags_added_file(tmp_path, monkeypatch):
    def git(*args):
        subprocess.run(
            ["git", "-c", "user.email=t@t", "-c", "user.name=t", *args],
            cwd=tmp_path, check=True, capture_output=True,
        )

    git("init", "-q", "-b", "main")
    (tmp_path / "a.txt").write_text("a")
    git("add", "-A")
    git("commit", "-qm", "base")
    _write(tmp_path / "big.bin", MAX_BYTES + 1)
    git("add", "-A")
    git("commit", "-qm", "add big")
    monkeypatch.chdir(tmp_path)
    assert main(["--base", "HEAD~1"], allowlist={}) == 1
    assert main(["--all"], allowlist={"big.bin": "test"}) == 0
