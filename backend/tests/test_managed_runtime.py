"""Fail-closed checks for CORSIA's on-demand executable cache."""
from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
import hashlib
import io
import threading
from pathlib import Path
import stat
import tarfile
import urllib.error
import zipfile

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
    observed_paths = []

    def fetch_plan(_command, *, path=None):
        observed_paths.append(path)
        return "node", pin

    monkeypatch.setattr(managed_runtime, "runtime_fetch_plan", fetch_plan)
    assert local_app_notice({"command": "npx", "env": {"PATH": "/app/bin"}}) == (
        f"{LOCAL_APP_NOTICE} CORSIA will download Node from nodejs.org to run it."
    )
    assert observed_paths == ["/app/bin"]
    monkeypatch.setattr(managed_runtime, "runtime_fetch_plan", lambda _command, *, path=None: ("gh", pin))
    assert local_app_notice({"command": "gh"}) == (
        f"{LOCAL_APP_NOTICE} CORSIA will download GitHub CLI from GitHub to run it."
    )
    monkeypatch.setattr(managed_runtime, "runtime_fetch_plan", lambda _command, *, path=None: None)
    assert local_app_notice({"command": "npx"}) == LOCAL_APP_NOTICE


@pytest.mark.parametrize(
    "member",
    [
        tarfile.TarInfo("/absolute"),
        tarfile.TarInfo("../escape"),
        tarfile.TarInfo("node/bin/link"),
    ],
)
def test_safe_tar_member_refuses_escaping_paths(member):
    from tools.managed_runtime import _safe_tar_member

    if member.name == "node/bin/link":
        member.type = tarfile.SYMTYPE
        member.linkname = "../../../escape"
    assert not _safe_tar_member(member)


def test_safe_tar_member_refuses_escaping_hardlink():
    from tools.managed_runtime import _safe_tar_member

    member = tarfile.TarInfo("node/bin/link")
    member.type = tarfile.LNKTYPE
    member.linkname = "../x"

    assert not _safe_tar_member(member)


def test_extract_refuses_unsafe_zip_symlink(tmp_path):
    from tools import managed_runtime

    archive = tmp_path / "unsafe.zip"
    info = zipfile.ZipInfo("node/bin/link")
    info.external_attr = (stat.S_IFLNK | 0o777) << 16
    with zipfile.ZipFile(archive, "w") as source:
        source.writestr(info, "../../../escape")
    pin = managed_runtime.RuntimePin("test", "https://example.invalid/test.zip", "a" * 64, "zip", "node/bin/node")

    with pytest.raises(managed_runtime.ManagedRuntimeError, match="unsafe path"):
        managed_runtime._extract(archive, tmp_path / "out", pin)


def test_invalid_read_only_install_is_replaced(monkeypatch, tmp_path):
    from tools import managed_runtime

    payload = _node_archive()
    pin = _pin(monkeypatch, payload)
    final = tmp_path / "runtimes" / "node" / pin.version / "arm64"
    final.mkdir(parents=True)
    (final / ".corsia-runtime.json").write_text("{}", encoding="utf-8")
    managed_runtime._make_read_execute_only(final)
    monkeypatch.setattr(managed_runtime.urllib.request, "urlopen", lambda *_args, **_kwargs: _Response(payload))

    executable = managed_runtime.ensure_runtime("node", root=tmp_path / "runtimes")

    assert executable.is_file()
    assert managed_runtime.runtime_is_installed("node", root=tmp_path / "runtimes")


def test_concurrent_installs_share_one_runtime_download(monkeypatch, tmp_path):
    from tools import managed_runtime

    payload = _node_archive()
    _pin(monkeypatch, payload)
    download_started = threading.Event()
    release_download = threading.Event()
    downloads = 0
    download_lock = threading.Lock()

    def delayed_download(*_args, **_kwargs):
        nonlocal downloads
        with download_lock:
            downloads += 1
            first = downloads == 1
        if first:
            download_started.set()
            assert release_download.wait(timeout=5)
        return _Response(payload)

    monkeypatch.setattr(managed_runtime.urllib.request, "urlopen", delayed_download)
    root = tmp_path / "runtimes"
    with ThreadPoolExecutor(max_workers=2) as executor:
        first = executor.submit(managed_runtime.ensure_runtime, "node", root=root)
        assert download_started.wait(timeout=5)
        second = executor.submit(managed_runtime.ensure_runtime, "node", root=root)
        release_download.set()
        first_path, second_path = first.result(timeout=5), second.result(timeout=5)

    assert first_path == second_path
    assert downloads == 1


def test_download_byte_cap_is_reported(monkeypatch, tmp_path):
    from tools import managed_runtime

    monkeypatch.setattr(managed_runtime, "_MAX_DOWNLOAD_BYTES", 3)
    monkeypatch.setattr(managed_runtime.urllib.request, "urlopen", lambda *_args, **_kwargs: _Response(b"four"))

    with pytest.raises(managed_runtime.ManagedRuntimeError, match="byte limit"):
        managed_runtime._download("https://example.invalid/archive", tmp_path / "download")


def test_persons_working_command_wins(monkeypatch, tmp_path):
    from tools import managed_runtime

    executable = tmp_path / "bin" / "npx"
    executable.parent.mkdir()
    executable.write_text("#!/bin/sh\n", encoding="utf-8")
    executable.chmod(0o755)
    monkeypatch.setattr(managed_runtime, "ensure_runtime", lambda *_args, **_kwargs: pytest.fail("must not download"))

    assert managed_runtime.command_path("npx", path=str(executable.parent)) == str(executable)
