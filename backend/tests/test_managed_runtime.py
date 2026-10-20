"""Fail-closed checks for CORSIA's on-demand executable cache."""
from __future__ import annotations

import hashlib
import io
from pathlib import Path
import tarfile
import urllib.error

import pytest


def _node_archive() -> bytes:
    contents = b"#!/bin/sh\necho node\n"
    buffer = io.BytesIO()
    with tarfile.open(fileobj=buffer, mode="w:gz") as archive:
        entry = tarfile.TarInfo("node/bin/node")
        entry.mode = 0o755
        entry.size = len(contents)
        archive.addfile(entry, io.BytesIO(contents))
        npx = tarfile.TarInfo("node/bin/npx")
        npx.mode = 0o755
        npx.size = len(contents)
        archive.addfile(npx, io.BytesIO(contents))
    return buffer.getvalue()


class _Response(io.BytesIO):
    def __enter__(self):
        return self

    def __exit__(self, *_args):
        self.close()


def _pin(monkeypatch, payload: bytes):
    from tools import managed_runtime

    pin = managed_runtime.RuntimePin("test", "https://nodejs.org/test.tar.gz", hashlib.sha256(payload).hexdigest(), "tar.gz", "node/bin/node")
    monkeypatch.setitem(managed_runtime.RUNTIME_PINS, "node", {"arm64": pin})
    monkeypatch.setattr(managed_runtime, "macos_arch", lambda: "arm64")
    return pin


def test_checksum_mismatch_never_publishes_a_runtime(monkeypatch, tmp_path):
    from tools import managed_runtime

    _pin(monkeypatch, b"expected")
    monkeypatch.setattr(managed_runtime.urllib.request, "urlopen", lambda *_args, **_kwargs: _Response(b"wrong"))

    with pytest.raises(managed_runtime.ManagedRuntimeError, match="checksum"):
        managed_runtime.ensure_runtime("node", root=tmp_path / "runtimes")

    assert not (tmp_path / "runtimes" / "node" / "test" / "arm64").exists()


def test_download_failure_never_publishes_a_runtime(monkeypatch, tmp_path):
    from tools import managed_runtime

    _pin(monkeypatch, _node_archive())
    monkeypatch.setattr(managed_runtime.urllib.request, "urlopen", lambda *_args, **_kwargs: (_ for _ in ()).throw(urllib.error.URLError("offline")))

    with pytest.raises(managed_runtime.ManagedRuntimeError, match="download failed"):
        managed_runtime.ensure_runtime("node", root=tmp_path / "runtimes")

    assert not (tmp_path / "runtimes" / "node" / "test" / "arm64").exists()


def test_finder_path_uses_the_pinned_npx(monkeypatch, tmp_path):
    from tools import managed_runtime

    payload = _node_archive()
    _pin(monkeypatch, payload)
    monkeypatch.setattr(managed_runtime.urllib.request, "urlopen", lambda *_args, **_kwargs: _Response(payload))

    command = Path(managed_runtime.command_path("npx", path="/usr/bin:/bin:/usr/sbin:/sbin", root=tmp_path / "runtimes"))

    assert command.name == "npx"
    assert command.is_file()
    assert command.is_absolute()


def test_approval_notice_names_only_a_pending_runtime_fetch(monkeypatch):
    from api.connected_app_rows import LOCAL_APP_NOTICE, local_app_notice
    from tools import managed_runtime

    pin = managed_runtime.RuntimePin("test", "https://nodejs.org/test.tar.gz", "a" * 64, "tar.gz", "node/bin/node")
    monkeypatch.setattr(managed_runtime, "runtime_fetch_plan", lambda _command: ("node", pin))
    assert local_app_notice({"command": "npx"}) == (
        f"{LOCAL_APP_NOTICE} CORSIA will download Node from nodejs.org to run it."
    )
    monkeypatch.setattr(managed_runtime, "runtime_fetch_plan", lambda _command: None)
    assert local_app_notice({"command": "npx"}) == LOCAL_APP_NOTICE


def test_persons_working_command_wins(monkeypatch, tmp_path):
    from tools import managed_runtime

    executable = tmp_path / "bin" / "npx"
    executable.parent.mkdir()
    executable.write_text("#!/bin/sh\n", encoding="utf-8")
    executable.chmod(0o755)
    monkeypatch.setattr(managed_runtime, "ensure_runtime", lambda *_args, **_kwargs: pytest.fail("must not download"))

    assert managed_runtime.command_path("npx", path=str(executable.parent)) == str(executable)
