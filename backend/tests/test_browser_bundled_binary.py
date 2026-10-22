"""The packaged app carries agent-browser, finds it first, and fetches its Chrome with no admin step."""
from __future__ import annotations

import json
from pathlib import Path
import re
import shutil
import sys

import pytest
from packaging.specifiers import SpecifierSet
from packaging.version import Version

from host_access import browser_cli as cli, browser_sessions as sessions
from tests.browser_process_fixture import synthetic_browser  # noqa: F401

_REPO = Path(__file__).resolve().parents[2]


def _bundle(tmp_path: Path, *, with_binary: bool) -> tuple[Path, Path]:
    bundle = tmp_path / "CORSIA.app"
    binary = bundle / "Contents" / "Resources" / "bin" / "agent-browser"
    binary.parent.mkdir(parents=True)
    if with_binary:
        shutil.copy(sys.executable, binary)  # any native executable stands in for the real build
    return bundle, binary


def test_the_bundled_binary_wins_over_path_and_still_passes_native_admission(tmp_path, monkeypatch):
    bundle, bundled = _bundle(tmp_path, with_binary=True)
    elsewhere = tmp_path / "path" / "agent-browser"
    elsewhere.parent.mkdir()
    shutil.copy(sys.executable, elsewhere)
    monkeypatch.setenv("PATH", str(elsewhere.parent))
    monkeypatch.setattr(cli, "app_bundle_root", lambda: bundle)
    assert cli.resolve_native("agent-browser")[0] == str(bundled.resolve())
    # Outside an app, PATH is still how a source install finds it.
    monkeypatch.setattr(cli, "app_bundle_root", lambda: None)
    assert cli.resolve_native("agent-browser")[0] == str(elsewhere.resolve())


def test_a_packaged_app_with_its_copy_missing_never_runs_one_from_path(tmp_path, monkeypatch):
    bundle, _ = _bundle(tmp_path, with_binary=False)
    elsewhere = tmp_path / "path" / "agent-browser"
    elsewhere.parent.mkdir()
    shutil.copy(sys.executable, elsewhere)
    monkeypatch.setenv("PATH", str(elsewhere.parent))
    monkeypatch.setattr(cli, "app_bundle_root", lambda: bundle)
    with pytest.raises(cli.identity.BrowserIdentityError, match="missing from this app") as missing:
        cli.resolve_native("agent-browser")
    assert "damaged" not in str(missing.value)


def test_a_bundled_script_is_still_refused(tmp_path, monkeypatch):
    bundle, bundled = _bundle(tmp_path, with_binary=False)
    bundled.write_text("#!/usr/bin/env node\n")
    bundled.chmod(0o755)
    monkeypatch.setattr(cli, "app_bundle_root", lambda: bundle)
    with pytest.raises(cli.identity.BrowserIdentityError, match="not a script or launcher"):
        cli.resolve_native("agent-browser")


def test_a_packaged_app_is_never_told_to_use_npm(tmp_path, monkeypatch):
    bundle, _ = _bundle(tmp_path, with_binary=False)
    monkeypatch.setattr(cli, "app_bundle_root", lambda: bundle)
    monkeypatch.setenv("PATH", str(tmp_path))
    with pytest.raises(cli.identity.BrowserIdentityError) as missing:
        cli.resolve_native("agent-browser")
    with pytest.raises(cli.identity.BrowserIdentityError) as wrong_version:
        cli.admitted_version("agent-browser 0.28.1")
    for error in (missing.value, wrong_version.value):
        assert "npm" not in str(error) and "reinstall or update CORSIA" in str(error)


@pytest.mark.asyncio
async def test_first_use_without_any_chrome_installs_it_once_and_a_failure_retries(synthetic_browser, monkeypatch, caplog):
    fake = synthetic_browser
    installs = []
    outcomes = [sessions.CommandResult(1, "", "offline"), sessions.CommandResult(0, "", "")]
    original = fake.owner._invoke

    async def invoke(argv, *, timeout):
        if argv[-1] == "install":
            installs.append((tuple(argv), timeout))
            return outcomes.pop(0)
        return await original(argv, timeout=timeout)

    monkeypatch.setattr(fake.owner, "_invoke", invoke)
    monkeypatch.setattr(cli, "chrome_download_missing", lambda: True)
    with pytest.raises(sessions.BrowserSetupError, match="could not download its browser") as caught:
        await fake.owner.run("thread", ["snapshot"])
    assert "npm" not in str(caught.value) and fake.owner.live_count() == 0
    assert "internet" not in str(caught.value) and "exit=1 stderr=offline" in caplog.text  # the real cause is recorded
    assert await fake.owner.run("thread", ["snapshot"]) == "ok"
    assert await fake.owner.run("thread", ["snapshot"]) == "ok"
    assert [argv[1:] for argv, _ in installs] == [("install",), ("install",)] and not outcomes
    assert installs[0][1] == cli.CHROME_INSTALL_TIMEOUT
    await fake.owner.shutdown()


@pytest.mark.asyncio
async def test_a_mac_with_chrome_or_a_downloaded_copy_installs_nothing(synthetic_browser, monkeypatch):
    fake = synthetic_browser
    original = fake.owner._invoke

    async def invoke(argv, *, timeout):
        assert argv[-1] != "install"
        return await original(argv, timeout=timeout)

    monkeypatch.setattr(fake.owner, "_invoke", invoke)
    await fake.owner.run("thread", ["snapshot"])  # the fixture reports a download already present
    monkeypatch.setattr(cli, "chrome_executable", lambda: ("/Applications/Google Chrome.app/x", "system"))
    monkeypatch.setattr(cli, "chrome_download_missing", lambda: pytest.fail("checked for a download despite Chrome"))
    fake.owner._native = None
    await fake.owner.run("thread", ["snapshot"])
    await fake.owner.shutdown()


def test_chrome_download_is_found_in_the_person_s_agent_browser_folder(tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path))
    assert cli.chrome_download_missing()
    (tmp_path / ".agent-browser" / "browsers" / "chrome-156.0.8078.47").mkdir(parents=True)
    assert not cli.chrome_download_missing()


def test_the_build_ships_the_pinned_binary_inside_a_verified_family():
    script = (_REPO / "desktop" / "scripts" / "build-macos-bundle.sh").read_text()
    version = re.search(r'^readonly AGENT_BROWSER_VERSION="([^"]+)"', script, re.MULTILINE).group(1)
    digest = re.search(r'^readonly AGENT_BROWSER_SHA256="([0-9a-f]{64})"', script, re.MULTILINE)
    url = re.search(r'^readonly AGENT_BROWSER_URL="([^"]+)"', script, re.MULTILINE).group(1)
    assert digest and "${AGENT_BROWSER_VERSION}/agent-browser-darwin-arm64" in url
    assert any(Version(version) in SpecifierSet(family) for family in cli._VERIFIED_FAMILIES)
    assert "fetch_verified \"$AGENT_BROWSER_URL\"" in script and "\nfetch_agent_browser\n" in script
    resources = json.loads((_REPO / "desktop" / "src-tauri" / "tauri.conf.json").read_text())["bundle"]["resources"]
    assert resources["../bundle/bin/agent-browser"] == "bin/agent-browser"
    assert re.search(r'^readonly AGENT_BROWSER_LICENSE_SHA256="[0-9a-f]{64}"$', script, re.MULTILINE)
    assert '"$BUNDLE_ROOT/licenses/agent-browser/LICENSE"' in script
    assert '"$RESOURCES/bin/agent-browser" --version' in (_REPO / "desktop" / "scripts" / "smoke-macos-bundle.sh").read_text()
