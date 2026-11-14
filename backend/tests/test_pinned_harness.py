"""CORSIA runs its own checked copy of Claude Code and Codex, never the person's."""
from __future__ import annotations

import asyncio
import hashlib
import io
import json
import tarfile
import threading
import urllib.error
import urllib.request
from pathlib import Path

import pytest

from tools import managed_runtime


class _Response(io.BytesIO):
    def __enter__(self):
        return self

    def __exit__(self, *_args):
        self.close()


def _claude_archive(body: bytes = b"#!/bin/sh\necho claude\n") -> bytes:
    buffer = io.BytesIO()
    with tarfile.open(fileobj=buffer, mode="w:gz") as archive:
        entry = tarfile.TarInfo("package/claude")
        entry.mode = 0o755
        entry.size = len(body)
        archive.addfile(entry, io.BytesIO(body))
    return buffer.getvalue()


def _sha512_pin(monkeypatch, payload: bytes, *, version: str = "2.0.0") -> managed_runtime.RuntimePin:
    pin = managed_runtime.RuntimePin(version, "https:" "//registry.test/claude.tgz", "", "tar.gz", "package/claude",
                                     hashlib.sha512(payload).hexdigest())
    monkeypatch.setitem(managed_runtime.RUNTIME_PINS, "claude-code", {"arm64": pin})
    monkeypatch.setattr(managed_runtime, "machine_key", lambda: "arm64")
    monkeypatch.setattr(managed_runtime, "macos_arch", lambda: "arm64")
    return pin


def test_every_supported_machine_has_a_sha512_pin_for_both_clis():
    for kind in ("claude-code", "codex"):
        pins = managed_runtime.RUNTIME_PINS[kind]
        assert set(pins) == {"arm64", "x86_64", "linux-x86_64", "linux-arm64"}
        for pin in pins.values():
            assert len(pin.sha512) == 128 and int(pin.sha512, 16) >= 0
            assert pin.sha256 == "" and pin.archive == "tar.gz"
            assert pin.url.startswith("https:" "//registry.npmjs.org/")
    assert managed_runtime.RUNTIME_PINS["codex"]["arm64"].executable == "package/vendor/aarch64-apple-darwin/bin/codex"
    assert managed_runtime.RUNTIME_PINS["claude-code"]["linux-x86_64"].executable == "package/claude"


def test_a_sha512_pin_installs_read_only_and_reads_installed(monkeypatch, tmp_path):
    payload = _claude_archive()
    pin = _sha512_pin(monkeypatch, payload)
    monkeypatch.setattr(urllib.request, "urlopen", lambda *_a, **_k: _Response(payload))
    root = tmp_path / "runtimes"

    executable = managed_runtime.ensure_runtime("claude-code", root=root)

    assert executable == root / "claude-code" / pin.version / "arm64" / "package" / "claude"
    assert executable.read_bytes().startswith(b"#!/bin/sh")
    assert not (executable.stat().st_mode & 0o222)
    assert managed_runtime.installed_runtime_path("claude-code", root=root) == executable
    marker = json.loads((executable.parents[1] / ".corsia-runtime.json").read_text())
    assert marker["sha512"] == pin.sha512


def test_a_tampered_archive_is_refused_and_nothing_is_installed(monkeypatch, tmp_path):
    _sha512_pin(monkeypatch, _claude_archive())
    tampered = _claude_archive(b"#!/bin/sh\necho something else\n")
    monkeypatch.setattr(urllib.request, "urlopen", lambda *_a, **_k: _Response(tampered))
    root = tmp_path / "runtimes"

    with pytest.raises(managed_runtime.ManagedRuntimeDownloadError, match="checksum"):
        managed_runtime.ensure_runtime("claude-code", root=root)

    assert managed_runtime.installed_runtime_path("claude-code", root=root) is None
    assert list((root / "claude-code").rglob("claude")) == []


def test_an_offline_first_run_leaves_the_cli_not_installed_with_no_fallback(monkeypatch, tmp_path):
    from connections import binary_from_config, stored_binary

    _sha512_pin(monkeypatch, _claude_archive())
    monkeypatch.setattr(urllib.request, "urlopen",
                        lambda *_a, **_k: (_ for _ in ()).throw(urllib.error.URLError("offline")))
    monkeypatch.setattr("shutil.which", lambda name, *_a, **_k: f"/usr/local/bin/{name}")
    root = tmp_path / "runtimes"

    with pytest.raises(managed_runtime.ManagedRuntimeDownloadError):
        managed_runtime.ensure_runtime("claude-code", root=root)

    assert binary_from_config("claude-code", {"binary": "/usr/local/bin/claude"}, candidate_roots=(root,)) is None
    monkeypatch.setattr(managed_runtime, "pinned_executable_path", lambda kind, **_k: root / kind / "expected")
    assert stored_binary("claude-code", None) == str(root / "claude-code" / "expected")


def test_existing_sha256_runtime_markers_stay_valid(monkeypatch, tmp_path):
    """A node install made before sha512 pins existed must not reinstall: its marker has no sha512 key."""
    pin = managed_runtime.RuntimePin("v1", "https:" "//nodejs.test/node.tgz", "a" * 64, "tar.gz", "node/bin/node")
    monkeypatch.setitem(managed_runtime.RUNTIME_PINS, "node", {"arm64": pin})
    install = tmp_path / "node" / "v1" / "arm64"
    (install / "node" / "bin").mkdir(parents=True)
    (install / "node" / "bin" / "node").write_text("#!/bin/sh\n")
    (install / "node" / "bin" / "node").chmod(0o755)
    old_marker = {"kind": "node", "arch": "arm64", "version": "v1", "url": pin.url, "sha256": pin.sha256,
                  "archive": "tar.gz", "executable": "node/bin/node"}
    (install / ".corsia-runtime.json").write_text(json.dumps(old_marker))

    assert managed_runtime.runtime_is_installed("node", root=tmp_path, arch="arm64")


def test_older_versions_are_removed_and_the_current_one_kept(monkeypatch, tmp_path):
    payload = _claude_archive()
    _sha512_pin(monkeypatch, payload, version="2.0.0")
    monkeypatch.setattr(urllib.request, "urlopen", lambda *_a, **_k: _Response(payload))
    root = tmp_path / "runtimes"
    old = root / "claude-code" / "1.9.0" / "arm64"
    old.mkdir(parents=True)
    (old / "claude").write_text("old")
    old.chmod(0o555)

    current = managed_runtime.ensure_runtime("claude-code", root=root)
    managed_runtime.remove_other_versions("claude-code", root=root)

    assert not (root / "claude-code" / "1.9.0").exists()
    assert current.is_file()


def test_claude_never_updates_itself(monkeypatch):
    from subagents.cc_env import clean_claude_env

    monkeypatch.delenv("DISABLE_UPDATES", raising=False)
    env = clean_claude_env()
    assert env["DISABLE_UPDATES"] == "1" and env["DISABLE_AUTOUPDATER"] == "1"


@pytest.mark.parametrize(("had_copy", "persons_copy", "accounts", "installs", "rechecks"), [
    (False, None, [], False, False),
    (False, "/usr/local/bin/claude", [], True, True),
    (False, None, [{"id": "a", "state": "signed_in"}], True, True),
    # The legacy Default every migrated install has, and a signed-out account, are not use.
    (False, None, [{"id": "a", "state": "legacy"}, {"id": "b", "state": "signed_out"}], False, False),
    # An installed copy only drops older versions; startup discovery already read its catalogs.
    (True, None, [], True, False),
])
def test_startup_installs_only_for_a_vendor_in_use(monkeypatch, had_copy, persons_copy, accounts, installs, rechecks):
    import cli_location
    from connections import pinned_harness, source_state, vendor_accounts

    installed, rechecked = [], []
    monkeypatch.setattr(managed_runtime, "installed_runtime_path",
                        lambda kind, **_k: Path("/pinned/claude") if had_copy and kind == "claude-code" else None)
    monkeypatch.setattr(cli_location, "service_binary", lambda vendor: persons_copy if vendor == "claude-code" else None)
    monkeypatch.setattr(vendor_accounts, "list_accounts",
                        lambda *, provider=None, db=None, **_k: accounts if provider == "claude-code" else [])
    monkeypatch.setattr(pinned_harness, "install_pinned", lambda vendor: installed.append(vendor) or Path("/x"))

    async def recheck(vendor):
        rechecked.append(vendor)

    monkeypatch.setattr(source_state, "check_harness_source", recheck)

    async def inline(fn, *args, **kwargs):
        return fn(*args, **kwargs)

    monkeypatch.setattr("infra.db_async.run_in_db", inline)
    asyncio.run(pinned_harness.install_used_harnesses())

    assert installed == (["claude-code"] if installs else [])
    assert rechecked == (["claude-code"] if rechecks else [])


@pytest.mark.parametrize(("test_mode", "scheduled"), [(None, True), ("1", False)])
def test_startup_schedules_the_install_off_its_path_and_tests_fetch_nothing(monkeypatch, test_mode, scheduled):
    import app_startup_runtime
    import infra.app_loop as app_loop
    from connections import pinned_harness

    submitted = []

    async def install():
        raise AssertionError("startup must not await the install")

    def submit(coro):
        submitted.append(coro)
        coro.close()

    if test_mode is None:
        monkeypatch.delenv("CORSIA_TEST_MODE", raising=False)
    else:
        monkeypatch.setenv("CORSIA_TEST_MODE", test_mode)
    monkeypatch.setattr(pinned_harness, "install_used_harnesses", install)
    monkeypatch.setattr(app_loop, "submit", submit)

    app_startup_runtime._schedule_pinned_harness_install()

    assert len(submitted) == (1 if scheduled else 0)


@pytest.mark.parametrize(("in_use", "installs"), [(True, True), (False, False)])
def test_a_recheck_installs_a_missing_copy_in_the_background(monkeypatch, in_use, installs):
    """A failed startup download or a moved pin recovers on the next re-check, without a
    restart, and the re-check returns while the download is still running."""
    from connections import pinned_harness, source_state
    from infra.state import ws

    present, notified, refreshed = [], [], []
    downloading, release = threading.Event(), threading.Event()

    def install(vendor):
        downloading.set()
        assert release.wait(timeout=5)
        present.append(vendor)
        return Path("/pinned/claude")

    monkeypatch.setattr(managed_runtime, "installed_runtime_path",
                        lambda kind, **_k: Path("/pinned/claude") if present else None)
    monkeypatch.setattr(pinned_harness, "vendor_in_use", lambda vendor, **_k: in_use)
    monkeypatch.setattr(pinned_harness, "install_pinned", install)
    monkeypatch.setattr(source_state, "record_install", lambda provider: "/pinned/claude" if present else None)
    monkeypatch.setattr(ws, "schedule_models_changed", lambda: notified.append(True))

    async def refresh(provider, **kwargs):
        refreshed.append(kwargs["binary"])

    monkeypatch.setattr("connections.harness_models.refresh_provider", refresh)

    async def inline(fn, *args, **kwargs):
        return fn(*args, **kwargs)

    monkeypatch.setattr(source_state, "run_in_db", inline)
    monkeypatch.setattr("infra.db_async.run_in_db", inline)

    async def scenario():
        await source_state.check_harness_source("claude-code")
        if installs:
            assert await asyncio.to_thread(downloading.wait, 5)
        assert present == [] and refreshed == []  # the re-check did not wait on the download
        release.set()
        await pinned_harness._installs["claude-code"]

    asyncio.run(scenario())

    assert present == (["claude-code"] if installs else [])
    assert refreshed == (["/pinned/claude"] if installs else [])
    assert notified == ([True] if installs else [])


def test_rechecks_during_a_download_join_it(monkeypatch):
    from connections import pinned_harness

    release, started = threading.Event(), []

    def install(vendor):
        started.append(vendor)
        assert release.wait(timeout=5)
        return Path("/pinned/claude")

    monkeypatch.setattr(pinned_harness, "vendor_in_use", lambda vendor, **_k: True)
    monkeypatch.setattr(pinned_harness, "install_pinned", install)

    async def no_check(_vendor):
        return None

    monkeypatch.setattr("connections.source_state.check_harness_source", no_check)
    monkeypatch.setattr("infra.state.ws.schedule_models_changed", lambda: None)

    async def inline(fn, *args, **kwargs):
        return fn(*args, **kwargs)

    monkeypatch.setattr("infra.db_async.run_in_db", inline)

    async def scenario():
        first = pinned_harness.start_install("claude-code")
        assert pinned_harness.start_install("claude-code") is first
        release.set()
        await first

    asyncio.run(scenario())
    assert started == ["claude-code"]


def test_a_waiting_install_stops_on_cancel_while_another_holds_the_lock(tmp_path):
    lock = managed_runtime._runtime_lock("claude-code")
    assert lock.acquire(timeout=1)
    try:
        with pytest.raises(managed_runtime.ManagedRuntimeCancelled):
            managed_runtime.ensure_runtime("claude-code", root=tmp_path, should_stop=lambda: True)
    finally:
        lock.release()


def test_a_recheck_whose_download_fails_records_not_installed(monkeypatch, caplog):
    """Offline, a re-check still records the row, so the other sources' results stand."""
    from connections import pinned_harness, source_state

    monkeypatch.setattr(pinned_harness, "vendor_in_use", lambda vendor, **_k: True)

    def offline(_vendor):
        raise managed_runtime.ManagedRuntimeDownloadError("download failed: URLError")

    monkeypatch.setattr(pinned_harness, "install_pinned", offline)
    recorded = []
    monkeypatch.setattr(source_state, "record_install", lambda provider: recorded.append(provider))

    async def inline(fn, *args, **kwargs):
        return fn(*args, **kwargs)

    monkeypatch.setattr(source_state, "run_in_db", inline)
    monkeypatch.setattr("infra.db_async.run_in_db", inline)

    async def scenario():
        await source_state.check_harness_source("claude-code")
        await pinned_harness._installs["claude-code"]

    with caplog.at_level("WARNING", logger=pinned_harness.log.name):
        asyncio.run(scenario())
    assert recorded == ["claude-code"]
    assert "could not set up CORSIA's claude-code copy" in caplog.text


def test_a_startup_install_failure_is_logged_and_startup_continues(monkeypatch, caplog):
    from connections import pinned_harness

    monkeypatch.setattr(pinned_harness, "vendor_in_use", lambda vendor, **_k: True)

    def offline(_vendor):
        raise managed_runtime.ManagedRuntimeDownloadError("download failed: URLError")

    monkeypatch.setattr(pinned_harness, "install_pinned", offline)

    async def inline(fn, *args, **kwargs):
        return fn(*args, **kwargs)

    monkeypatch.setattr("infra.db_async.run_in_db", inline)
    with caplog.at_level("WARNING", logger=pinned_harness.log.name):
        asyncio.run(pinned_harness.install_used_harnesses())
    assert "could not set up CORSIA's claude-code copy" in caplog.text
    assert "could not set up CORSIA's codex copy" in caplog.text
