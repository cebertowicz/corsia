from __future__ import annotations

import subprocess
from unittest.mock import patch

import pytest


def _status(monkeypatch, replies, fetch_error=None):
    from infra import update_status as mod

    mod.invalidate_cache()

    def fake_git(args, timeout=30):
        key = tuple(args)
        if fetch_error is not None and key[0] == "fetch":
            raise fetch_error
        if key not in replies:
            raise RuntimeError(f"unexpected git call: {key}")
        return replies[key]

    monkeypatch.setattr(mod, "_git", fake_git)
    return mod


_ON_BRANCH = {
    ("symbolic-ref", "--quiet", "--short", "HEAD"): "dev",
    ("rev-parse", "HEAD"): "a" * 40,
    ("fetch", "--quiet", "origin", "dev"): "",
    ("rev-parse", "origin/dev"): "b" * 40,
    ("rev-list", "--left-right", "--count", "HEAD...origin/dev"): "0\t3",
}


def test_reports_available_when_remote_is_ahead(monkeypatch):
    mod = _status(monkeypatch, _ON_BRANCH)
    status = mod.update_status(force=True)

    assert status.available is True
    assert status.commits_behind == 3
    assert status.branch == "dev"
    assert status.error is None


def test_reports_up_to_date_when_shas_match(monkeypatch):
    replies = dict(_ON_BRANCH)
    replies[("rev-parse", "origin/dev")] = "a" * 40
    replies[("rev-list", "--left-right", "--count", "HEAD...origin/dev")] = "0\t0"
    mod = _status(monkeypatch, replies)

    status = mod.update_status(force=True)

    assert status.available is False
    assert status.commits_behind == 0


def test_unreachable_remote_is_unknown_not_up_to_date(monkeypatch):
    # A failed fetch must never render as "up to date" — that would hide a real
    # update behind a network blip.
    mod = _status(monkeypatch, _ON_BRANCH, fetch_error=RuntimeError("offline"))

    status = mod.update_status(force=True)

    assert status.available is None
    assert "offline" in status.error
    assert status.checked_at is not None


def test_fetch_timeout_is_unknown(monkeypatch):
    mod = _status(
        monkeypatch,
        _ON_BRANCH,
        fetch_error=subprocess.TimeoutExpired(cmd="git fetch", timeout=30),
    )

    status = mod.update_status(force=True)

    assert status.available is None
    assert "timed out" in status.error


def test_detached_head_is_unknown(monkeypatch):
    from infra import update_status as mod

    mod.invalidate_cache()

    def fake_git(args, timeout=30):
        raise RuntimeError("not a symbolic ref")

    monkeypatch.setattr(mod, "_git", fake_git)
    status = mod.update_status(force=True)

    assert status.available is None
    assert "detached" in status.error.lower()


def test_cache_prevents_repeated_remote_calls(monkeypatch):
    # Poll frequency in the browser must not drive how often the remote is hit.
    from infra import update_status as mod

    mod.invalidate_cache()
    calls = {"n": 0}

    def counting_git(args, timeout=30):
        if args[0] == "fetch":
            calls["n"] += 1
        return _ON_BRANCH[tuple(args)]

    monkeypatch.setattr(mod, "_git", counting_git)

    for _ in range(10):
        mod.update_status()

    assert calls["n"] == 1

    mod.update_status(force=True)
    assert calls["n"] == 2


def test_locally_ahead_branch_is_not_an_update(monkeypatch):
    # Local commits not yet pushed make HEAD differ from the remote ref, but
    # there is nothing to pull. This must not offer an update.
    replies = dict(_ON_BRANCH)
    replies[("rev-list", "--left-right", "--count", "HEAD...origin/dev")] = "2\t0"
    mod = _status(monkeypatch, replies)

    status = mod.update_status(force=True)

    assert status.available is False
    assert status.commits_behind == 0
    assert status.current_sha != status.remote_sha


def test_diverged_branch_with_remote_commits_is_an_update(monkeypatch):
    # Ahead *and* behind: there are still remote commits to pull.
    replies = dict(_ON_BRANCH)
    replies[("rev-list", "--left-right", "--count", "HEAD...origin/dev")] = "2\t5"
    mod = _status(monkeypatch, replies)

    status = mod.update_status(force=True)

    assert status.available is True
    assert status.commits_behind == 5


def test_unusable_behind_count_is_unknown(monkeypatch):
    # Without a usable count we cannot tell ahead from behind, so neither
    # "update available" nor "up to date" may be claimed.
    replies = dict(_ON_BRANCH)
    replies[("rev-list", "--left-right", "--count", "HEAD...origin/dev")] = "garbage"
    mod = _status(monkeypatch, replies)

    status = mod.update_status(force=True)

    assert status.available is None
    assert status.commits_behind is None
    assert "how far behind" in status.error


class _ManifestResponse:
    def __init__(self, body: bytes):
        self.body = body

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        return False

    def read(self) -> bytes:
        return self.body


def _packaged_status(monkeypatch, tmp_path, *, current_version="1.0.0", manifest=None):
    import json

    from infra import update_status as mod

    bundle = tmp_path / "CORSIA.app"
    resource = bundle / "Contents" / "Resources" / "updater-endpoint.json"
    resource.parent.mkdir(parents=True)
    resource.write_text(json.dumps({"endpoint": "https://updates.example.test/latest.json"}))
    body = manifest if isinstance(manifest, bytes) else json.dumps(manifest or {"version": "1.1.0"}).encode()
    mod.invalidate_cache()
    monkeypatch.setattr(mod.platform_paths, "app_bundle_root", lambda: bundle)
    monkeypatch.setattr(mod, "_git", lambda *_args, **_kwargs: pytest.fail("packaged check must not call git"))
    monkeypatch.setattr(mod, "_bundle_current_version", lambda: current_version)
    monkeypatch.setattr(mod, "urlopen", lambda _endpoint, timeout: _ManifestResponse(body))
    return mod.update_status(force=True)


def test_packaged_app_reports_newer_manifest_version(monkeypatch, tmp_path):
    status = _packaged_status(monkeypatch, tmp_path, manifest={"version": "1.1.0"})

    assert status.available is True
    assert status.current_version == "1.0.0"
    assert status.target_version == "1.1.0"
    assert status.changes is None


@pytest.mark.parametrize("target_version", ["1.0.0", "0.9.0"])
def test_packaged_app_reports_equal_or_older_version_as_up_to_date(monkeypatch, tmp_path, target_version):
    status = _packaged_status(monkeypatch, tmp_path, manifest={"version": target_version})

    assert status.available is False
    assert status.target_version == target_version


def test_packaged_app_unreachable_manifest_is_unknown(monkeypatch, tmp_path):
    from urllib.error import URLError

    from infra import update_status as mod

    bundle = tmp_path / "CORSIA.app"
    resource = bundle / "Contents" / "Resources" / "updater-endpoint.json"
    resource.parent.mkdir(parents=True)
    resource.write_text('{"endpoint": "https://updates.example.test/latest.json"}')
    mod.invalidate_cache()
    monkeypatch.setattr(mod.platform_paths, "app_bundle_root", lambda: bundle)
    monkeypatch.setattr(mod, "_git", lambda *_args, **_kwargs: pytest.fail("packaged check must not call git"))
    monkeypatch.setattr(mod, "_bundle_current_version", lambda: "1.0.0")
    monkeypatch.setattr(mod, "urlopen", lambda *_args, **_kwargs: (_ for _ in ()).throw(URLError("offline")))

    status = mod.update_status(force=True)

    assert status.available is None
    assert "offline" in status.error


@pytest.mark.parametrize(
    "manifest",
    [b"{not-json", {"version": "not-semver"}],
    ids=["malformed-json", "non-semver"],
)
def test_packaged_app_malformed_or_non_semver_manifest_is_unknown(monkeypatch, tmp_path, manifest):
    status = _packaged_status(monkeypatch, tmp_path, manifest=manifest)

    assert status.available is None
    assert status.error


def test_packaged_manifest_notes_cap_and_count_more(monkeypatch, tmp_path):
    notes = "\n".join(f"- Change {index}" for index in range(52))
    status = _packaged_status(monkeypatch, tmp_path, manifest={"version": "1.1.0", "notes": notes})

    assert status.changes == {
        "kind": "changelog",
        "headline": None,
        "items": [f"Change {index}" for index in range(50)],
        "more": 2,
    }


def test_packaged_manifest_empty_notes_are_absent(monkeypatch, tmp_path):
    status = _packaged_status(monkeypatch, tmp_path, manifest={"version": "1.1.0", "notes": " \n\n "})

    assert status.changes is None


def test_source_checkout_still_uses_git(monkeypatch):
    from infra import update_status as mod

    mod.invalidate_cache()
    calls = []

    def fake_git(args, timeout=30):
        calls.append(tuple(args))
        return _ON_BRANCH[tuple(args)]

    monkeypatch.setattr(mod.platform_paths, "app_bundle_root", lambda: None)
    monkeypatch.setattr(mod, "_git", fake_git)
    assert mod.update_status(force=True).available is True
    assert ("fetch", "--quiet", "origin", "dev") in calls
