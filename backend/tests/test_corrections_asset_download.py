"""First-run classifier download: resume, integrity, disk, atomic install, idempotence.

The flight talks to a real local HTTP server serving a tiny fixture asset; no test
downloads the real model.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import sys
import threading
import time
from dataclasses import replace
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from core import corrections_asset_download as download  # noqa: E402
from core import corrections_asset_files, corrections_asset_pins, corrections_classifier  # noqa: E402
from core import corrections_asset_signals as signals  # noqa: E402
from core.onnx_asset import AssetError  # noqa: E402
from infra.default_port import DEFAULT_BACKEND_PORT  # noqa: E402


def _sha(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _build_asset(seed: str = "a", *, version: str = "v1") -> dict[str, bytes]:
    files = {
        "LICENSE": f"license {seed}".encode(),
        "NOTICE": f"notice {seed}".encode(),
        "model.onnx": f"graph {seed}".encode(),
        "model.onnx.data": (seed.encode() * 4096) * 40,  # 160 KiB: big enough to cut mid-body
        "tokenizer.json": b'{"tokenizer": true}',
        "tokenizer_config.json": b"{}",
    }
    manifest = {
        "asset_id": "corrections-classifier",
        "version": version,
        "input_policy_id": "modernbert-owner-context-v2-2048",
        "files": {name: {"bytes": len(data), "sha256": _sha(data)} for name, data in files.items()},
        "heads": [{"name": "correction", "threshold": 0.5, "labels": ["not_correction", "correction"]}],
        "self_test": [{"context": [], "owner": f"t{index}", "expected_score": 0.5} for index in range(4)],
    }
    return {**files, "manifest.json": json.dumps(manifest, sort_keys=True).encode()}


class _Server:
    def __init__(self, assets: dict[str, bytes]):
        self.assets = assets
        self.requests: list[tuple[str, str | None]] = []
        self.cut_once: dict[str, int] = {}
        self.corrupt: set[str] = set()
        self.corrupt_once: set[str] = set()
        self.busy_once: set[str] = set()
        outer = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *_args):  # noqa: D401
                pass

            def do_GET(self):
                name = self.path.lstrip("/")
                outer.requests.append((name, self.headers.get("Range")))
                data = outer.assets.get(name)
                if data is None:
                    self.send_response(404)
                    self.end_headers()
                    return
                if name in outer.busy_once:
                    outer.busy_once.discard(name)
                    self.send_response(503)
                    self.end_headers()
                    return
                if name in outer.corrupt or name in outer.corrupt_once:
                    outer.corrupt_once.discard(name)
                    data = bytes(len(data))
                start, status = 0, 200
                match = re.match(r"bytes=(\d+)-", self.headers.get("Range") or "")
                if match:
                    start, status = int(match.group(1)), 206
                body = data[start:]
                self.send_response(status)
                self.send_header("Content-Length", str(len(body)))
                if status == 206:
                    self.send_header("Content-Range", f"bytes {start}-{len(data) - 1}/{len(data)}")
                self.end_headers()
                cut = outer.cut_once.pop(name, None)
                if cut is not None:
                    self.wfile.write(body[:cut])
                    self.wfile.flush()
                    self.close_connection = True
                    return
                self.wfile.write(body)

        self.httpd = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.thread = threading.Thread(target=self.httpd.serve_forever, daemon=True)
        self.thread.start()
        self.url = f"http://127.0.0.1:{self.httpd.server_address[1]}"

    def close(self):
        self.httpd.shutdown()
        self.httpd.server_close()

    def requested(self, name: str) -> list[str | None]:
        return [header for requested, header in self.requests if requested == name]


@pytest.fixture
def assets():
    return _build_asset("a")


@pytest.fixture
def server(assets):
    instance = _Server(assets)
    yield instance
    instance.close()


@pytest.fixture
def pinned(monkeypatch, assets):
    pins = {"v1": _sha(assets["manifest.json"])}
    monkeypatch.setattr(corrections_asset_pins, "PINNED_MANIFESTS", pins, raising=True)
    monkeypatch.setattr(corrections_asset_files, "PINNED_MANIFESTS", pins)
    monkeypatch.setattr(download, "PINNED_MANIFESTS", pins)
    monkeypatch.setattr(
        corrections_classifier, "CORRECTIONS_SPEC",
        replace(corrections_classifier.CORRECTIONS_SPEC, pins=pins),
    )


def _flight(tmp_path, server, *, self_test=None, free_bytes=None, root=None, notifications=None, woken=None, **options):
    calls: list[Path] = []

    def passing(path: Path) -> None:
        calls.append(path)

    return download.ClassifierDownload(
        root=lambda: root or tmp_path / "asset",
        url=lambda: server.url,
        self_test=self_test or passing,
        free_bytes=free_bytes or (lambda _path: 10**12),
        sleep=lambda _seconds: None,
        notify=(lambda: notifications.append(None)) if notifications is not None else (lambda: None),
        wake=(lambda: woken.append(None)) if woken is not None else (lambda: None),
        **{"space_recheck_seconds": 3600.0, **options},
    ), calls


def test_downloads_installs_and_self_tests(tmp_path, server, assets, pinned):
    flight, tested = _flight(tmp_path, server)

    state = flight.start(wait=True)

    assert state == {"state": "ready"}
    root = tmp_path / "asset"
    for name, data in assets.items():
        assert (root / name).read_bytes() == data
    assert tested == [root]
    assert not download.staging_dir(root).exists() and not download.previous_dir(root).exists()


def test_resumes_an_interrupted_file_with_a_range_request(tmp_path, server, assets, pinned):
    server.cut_once["model.onnx.data"] = 50_000
    flight, _ = _flight(tmp_path, server)

    assert flight.start(wait=True) == {"state": "ready"}

    requests = server.requested("model.onnx.data")
    assert requests[0] is None
    assert requests[1] == "bytes=50000-"
    assert (tmp_path / "asset" / "model.onnx.data").read_bytes() == assets["model.onnx.data"]


def test_clients_are_nudged_on_state_changes_not_on_every_chunk(tmp_path, server, pinned):
    nudges: list[None] = []
    flight, _ = _flight(tmp_path, server, notifications=nudges)
    nudges.clear()

    assert flight.start(wait=True) == {"state": "ready"}

    # downloading, verifying, self_testing and ready each announce themselves; chunks inside a second do not.
    assert 3 <= len(nudges) <= 12


def test_a_busy_server_is_retried_not_failed(tmp_path, server, pinned):
    server.busy_once.add("model.onnx.data")
    flight, _ = _flight(tmp_path, server)

    assert flight.start(wait=True) == {"state": "ready"}
    assert len(server.requested("model.onnx.data")) == 2


def test_staged_partial_survives_a_restart_and_resumes(tmp_path, server, assets, pinned):
    staging = download.staging_dir(tmp_path / "asset")
    staging.mkdir(parents=True)
    (staging / "model.onnx.data").write_bytes(assets["model.onnx.data"][:1000])
    flight, _ = _flight(tmp_path, server)

    assert flight.start(wait=True) == {"state": "ready"}
    assert server.requested("model.onnx.data") == ["bytes=1000-"]


def _old_install_and_new_pin(tmp_path, monkeypatch):
    """A verified v1 on disk while the source now pins v2; returns (root, old, new)."""
    old_assets = _build_asset("old", version="v1")
    new_assets = _build_asset("new", version="v2")
    pins = {"v1": _sha(old_assets["manifest.json"]), "v2": _sha(new_assets["manifest.json"])}
    monkeypatch.setattr(corrections_asset_files, "PINNED_MANIFESTS", pins)
    monkeypatch.setattr(download, "PINNED_MANIFESTS", pins)
    monkeypatch.setattr(
        corrections_classifier, "CORRECTIONS_SPEC",
        replace(corrections_classifier.CORRECTIONS_SPEC, pins=pins),
    )
    root = tmp_path / "asset"
    root.mkdir()
    for name, data in old_assets.items():
        (root / name).write_bytes(data)
    return root, old_assets, new_assets


def test_current_asset_verified_is_true_only_for_the_newest_pin(tmp_path, monkeypatch):
    root, old_assets, new_assets = _old_install_and_new_pin(tmp_path, monkeypatch)

    assert corrections_asset_files.current_asset_verified(tmp_path / "missing") is False
    assert corrections_asset_files.current_asset_verified(root) is False  # v1 verifies, v2 is pinned

    for name, data in new_assets.items():
        (root / name).write_bytes(data)
    assert corrections_asset_files.current_asset_verified(root) is True

    (root / "model.onnx.data").write_bytes(b"damaged")
    assert corrections_asset_files.current_asset_verified(root) is False


def test_the_newest_pin_is_ordered_by_number_not_text(tmp_path, monkeypatch):
    monkeypatch.setattr(corrections_asset_files, "PINNED_MANIFESTS", {"v9": "0" * 64, "v10": "1" * 64})
    root = tmp_path / "asset"
    root.mkdir()

    assert corrections_asset_files.newest_pin() == "v10"
    assert corrections_asset_files.staging_dir(root).name == ".asset.staging-v10"
    (root / "manifest.json").write_text(json.dumps({"version": "v9"}))
    assert corrections_asset_files.root_is_newest_pin(root) is False
    (root / "manifest.json").write_text(json.dumps({"version": "v10"}))
    assert corrections_asset_files.root_is_newest_pin(root) is True


def test_an_older_verified_version_is_replaced_and_keeps_serving_until_then(tmp_path, monkeypatch):
    root, old_assets, new_assets = _old_install_and_new_pin(tmp_path, monkeypatch)
    server = _Server(new_assets)
    parked_versions: list[str] = []

    def check(path: Path) -> None:
        parked = download.previous_dir(root) / "manifest.json"
        if parked.is_file():
            parked_versions.append(json.loads(parked.read_text())["version"])

    try:
        flight, _ = _flight(tmp_path, server, root=root, self_test=check)
        assert (root / "manifest.json").read_bytes() == old_assets["manifest.json"]
        assert flight.start(wait=True) == {"state": "ready"}
    finally:
        server.close()

    assert (root / "manifest.json").read_bytes() == new_assets["manifest.json"]
    assert parked_versions == ["v1"]
    assert not download.previous_dir(root).exists()


def test_a_process_killed_mid_download_restarts_cleanly_and_the_old_version_keeps_serving(tmp_path, monkeypatch):
    root, old_assets, new_assets = _old_install_and_new_pin(tmp_path, monkeypatch)
    server = _Server(new_assets)
    server.cut_once["model.onnx.data"] = 50_000

    def killed(_seconds: float) -> None:
        raise KeyboardInterrupt("process killed")  # not an Exception: nothing cleans up

    try:
        dying, _ = _flight(tmp_path, server, root=root)
        dying._sleep = killed
        with pytest.raises(KeyboardInterrupt):
            dying._flight()

        staging = download.staging_dir(root)
        assert (staging / "model.onnx.data").stat().st_size == 50_000
        for name, data in old_assets.items():  # the old version is untouched and whole
            assert (root / name).read_bytes() == data
        assert not download.previous_dir(root).exists()
        assert {path.name for path in tmp_path.iterdir()} == {"asset", staging.name}

        assert download.recover_interrupted_install(root) is False  # no swap was interrupted
        restarted, _ = _flight(tmp_path, server, root=root)
        assert restarted.start(wait=True) == {"state": "ready"}
    finally:
        server.close()

    assert server.requested("model.onnx.data")[-1] == "bytes=50000-"
    assert (root / "manifest.json").read_bytes() == new_assets["manifest.json"]
    assert {path.name for path in tmp_path.iterdir()} == {"asset"}


def test_checksum_mismatch_deletes_the_file_and_is_retryable(tmp_path, server, pinned):
    server.corrupt.add("model.onnx.data")
    flight, tested = _flight(tmp_path, server)

    state = download.public_state(flight.start(wait=True))

    assert state["state"] == "failed" and state["code"] == "checksum_failed" and state["retryable"] is True
    assert state["reason"] == "The download was damaged."
    assert len(server.requested("model.onnx.data")) == 2
    assert not (download.staging_dir(tmp_path / "asset") / "model.onnx.data").exists()
    assert not (tmp_path / "asset" / "manifest.json").exists()
    assert tested == []


def test_a_damaged_download_is_fetched_once_more_by_itself_and_never_reads_as_failed(tmp_path, server, pinned):
    server.corrupt_once.add("model.onnx.data")
    shown: list[str] = []
    flight, tested = _flight(tmp_path, server, notifications=shown)
    flight._notify = lambda: shown.append(flight.status()["state"])

    assert flight.start(wait=True) == {"state": "ready"}

    assert len(server.requested("model.onnx.data")) == 2
    assert "failed" not in shown
    assert shown.count("downloading") >= 2 and "verifying" in shown
    assert tested == [tmp_path / "asset"]


def test_unpinned_manifest_is_refused_before_any_file_is_fetched(tmp_path, server, monkeypatch, pinned):
    monkeypatch.setattr(download, "PINNED_MANIFESTS", {"v1": "0" * 64})
    flight, _ = _flight(tmp_path, server)

    state = flight.start(wait=True)

    assert state["state"] == "failed" and state["code"] == "manifest_mismatch"
    assert [name for name, _ in server.requests] == ["manifest.json"]
    assert not (download.staging_dir(tmp_path / "asset") / "manifest.json").exists()


def test_full_disk_reports_the_bytes_needed_and_fetches_nothing(tmp_path, server, assets, pinned):
    flight, _ = _flight(tmp_path, server, free_bytes=lambda _path: 1000)

    state = download.public_state(flight.start(wait=True))

    total = sum(len(data) for name, data in assets.items() if name != "manifest.json")
    assert state["code"] == "disk_full" and state["state"] == "failed"
    assert state["bytes_needed"] == total + download.DISK_MARGIN_BYTES
    assert state["reason"] == download.short_of_space(state["bytes_needed"] - 1000)
    assert state["reason"].startswith("Needs ") and state["reason"].endswith(" GB more free space.")
    assert [name for name, _ in server.requests] == ["manifest.json"]


@pytest.mark.parametrize(
    ("short_bytes", "sentence"),
    [
        (1, "Needs 0.1 GB more free space."),
        (100_000_000, "Needs 0.1 GB more free space."),
        (100_000_001, "Needs 0.2 GB more free space."),
        (900_000_000, "Needs 0.9 GB more free space."),
        (1_234_567_890, "Needs 1.3 GB more free space."),
        (12_000_000_000, "Needs 12.0 GB more free space."),
    ],
)
def test_the_space_sentence_is_what_is_missing_rounded_up_to_a_tenth(short_bytes, sentence):
    assert download.short_of_space(short_bytes) == sentence


def _wait_for(condition, seconds: float = 10.0) -> None:
    deadline = time.monotonic() + seconds
    while not condition():
        assert time.monotonic() < deadline, "timed out"
        time.sleep(0.01)


def test_a_full_disk_resumes_by_itself_once_there_is_room(tmp_path, server, pinned):
    free = {"bytes": 1000}
    woken: list[None] = []
    flight, _ = _flight(tmp_path, server, free_bytes=lambda _path: free["bytes"], woken=woken, space_recheck_seconds=0.02)

    assert flight.start(wait=True)["code"] == "disk_full"
    time.sleep(0.1)
    assert flight.status()["code"] == "disk_full" and [name for name, _ in server.requests] == ["manifest.json"]

    free["bytes"] = 10**12
    _wait_for(lambda: flight.status() == {"state": "ready"})
    assert woken == [None]


def test_the_setup_window_regaining_focus_resumes_a_full_disk_only_when_there_is_room(tmp_path, server, pinned):
    free = {"bytes": 1000}
    flight, _ = _flight(tmp_path, server, free_bytes=lambda _path: free["bytes"])
    state = flight.start(wait=True)
    needed = state["bytes_needed"]

    free["bytes"] = needed - 250_000_000
    still_short = flight.recheck_space()
    assert still_short["code"] == "disk_full" and still_short["reason"] == "Needs 0.3 GB more free space."
    assert [name for name, _ in server.requests] == ["manifest.json"]

    free["bytes"] = needed
    flight.recheck_space()
    flight._thread.join(10)
    assert flight.status() == {"state": "ready"}


def test_rechecking_space_does_nothing_unless_the_download_stopped_for_space(tmp_path, server, pinned):
    flight, _ = _flight(tmp_path, server)
    assert flight.recheck_space() == {"state": "idle"}

    server.corrupt.add("model.onnx.data")
    assert flight.start(wait=True)["code"] == "checksum_failed"
    requests = len(server.requests)
    assert flight.recheck_space()["code"] == "checksum_failed"
    assert len(server.requests) == requests


def _staged(tmp_path, server) -> Path:
    staging = download.staging_dir(tmp_path / "asset")
    staging.mkdir(parents=True)
    for name, data in server.assets.items():
        (staging / name).write_bytes(data)
    return staging


def _install_previous(root: Path) -> dict[str, bytes]:
    previous = _build_asset("p")
    root.mkdir(parents=True)
    for name, data in previous.items():
        (root / name).write_bytes(data)
    return previous


def _needs_new_manifest(new_manifest: bytes):
    def check(path: Path) -> None:
        if (path / "manifest.json").read_bytes() != new_manifest:
            raise AssetError("old", reader="The previous version is not the pinned one.")

    return check


def test_atomic_install_failure_keeps_the_previous_version(tmp_path, server, assets, pinned, monkeypatch):
    root = tmp_path / "asset"
    previous = _install_previous(root)
    real_replace = os.replace

    def failing(src, dst):
        if Path(dst) == root and Path(src) == download.staging_dir(root):
            raise OSError("disk went away")
        return real_replace(src, dst)

    monkeypatch.setattr(download.os, "replace", failing)
    flight, _ = _flight(tmp_path, server, self_test=_needs_new_manifest(assets["manifest.json"]))

    state = flight.start(wait=True)

    assert state["state"] == "failed" and state["code"] == "install_failed" and state["retryable"] is True
    for name, data in previous.items():
        assert (root / name).read_bytes() == data
    assert not download.previous_dir(root).exists()
    assert download.staging_dir(root).is_dir()


def test_self_test_failure_restores_the_previous_version(tmp_path, server, assets, pinned):
    root = tmp_path / "asset"
    previous = _install_previous(root)

    def failing(path: Path) -> None:
        if (path / "manifest.json").read_bytes() == assets["manifest.json"]:
            raise AssetError("bad", reader="The corrections classifier gave unexpected answers on its built-in checks. Retry setup.")
        raise AssetError("old", reader="old")

    flight, _ = _flight(tmp_path, server, self_test=failing)

    state = flight.start(wait=True)

    assert state["code"] == "self_test_failed"
    assert state["reason"] == "The download didn’t pass its checks."
    assert not download.previous_dir(root).exists()
    for name, data in previous.items():
        assert (root / name).read_bytes() == data
    assert not (root / "model.onnx.data").read_bytes() == assets["model.onnx.data"]


def test_the_old_version_serves_until_the_new_one_passes_its_self_test(tmp_path, server, assets, pinned):
    root = tmp_path / "asset"
    previous = _install_previous(root)
    seen: list[bytes] = []

    def spying(path: Path) -> None:
        manifest = (path / "manifest.json").read_bytes()
        seen.append(manifest)
        if manifest != assets["manifest.json"]:
            raise AssetError("old", reader="old")
        # While the new version is being tested the old one is parked, whole, beside the root.
        assert (download.previous_dir(root) / "manifest.json").read_bytes() == previous["manifest.json"]

    flight, _ = _flight(tmp_path, server, self_test=spying)

    assert flight.start(wait=True) == {"state": "ready"}
    assert seen[-1] == assets["manifest.json"]
    assert not download.previous_dir(root).exists()


def test_a_crash_between_the_two_renames_restores_the_previous_version_at_startup(tmp_path, server, pinned):
    root = tmp_path / "asset"
    previous = _install_previous(root)
    real_replace = os.replace

    def dies_after_parking(src, dst):
        if Path(dst) == root and Path(src) == download.staging_dir(root):
            raise KeyboardInterrupt("process killed")  # not an Exception: no rollback runs
        return real_replace(src, dst)

    flight, _ = _flight(tmp_path, server)
    with pytest.MonkeyPatch.context() as patch:
        patch.setattr(download.os, "replace", dies_after_parking)
        with pytest.raises(KeyboardInterrupt):
            flight._install(root, _staged(tmp_path, server))

    assert not root.exists() and download.previous_dir(root).is_dir()
    assert download.recover_interrupted_install(root) is True
    for name, data in previous.items():
        assert (root / name).read_bytes() == data
    assert not download.previous_dir(root).exists()
    assert download.recover_interrupted_install(root) is False


def test_a_swap_that_died_before_its_self_test_falls_back_to_the_parked_version(tmp_path, server, assets, pinned):
    root = tmp_path / "asset"
    previous = _install_previous(root)
    parked = download.previous_dir(root)
    root.rename(parked)  # the old version, parked ...
    root.mkdir()  # ... and an untested new root that never passed
    (root / "manifest.json").write_bytes(b"{}")

    def only_the_parked_one_passes(path: Path) -> None:
        if (path / "manifest.json").read_bytes() != previous["manifest.json"]:
            raise AssetError("bad", reader="bad")

    flight, _ = _flight(tmp_path, server, self_test=only_the_parked_one_passes)

    assert flight.start(wait=True) == {"state": "ready"}
    assert (root / "manifest.json").read_bytes() == previous["manifest.json"]
    assert server.requests == []
    assert not parked.exists()


def test_no_failure_sentence_repeats_the_action_that_sits_under_it(tmp_path, server, pinned, monkeypatch):
    sentences = []
    server.corrupt.add("model.onnx.data")
    flight, _ = _flight(tmp_path, server)
    sentences.append(flight.start(wait=True)["reason"])
    flight, _ = _flight(tmp_path, server, free_bytes=lambda _path: 1)
    sentences.append(flight.start(wait=True)["reason"])
    monkeypatch.setattr(download, "PINNED_MANIFESTS", {"v1": "0" * 64})
    flight, _ = _flight(tmp_path, server)
    sentences.append(flight.start(wait=True)["reason"])
    sentences.append(download._reader(AssetError("x", reader="The corrections classifier is damaged. Retry setup.")))

    assert len(sentences) == 4 and all(sentences)
    assert not any("try again" in text.lower() for text in sentences)
    assert "classifier" not in sentences[-1].lower() and "correction" not in sentences[-1].lower()


def test_no_failure_sentence_a_person_can_see_names_the_classifier(tmp_path, server, pinned, monkeypatch):
    sentences = []
    server.corrupt.add("model.onnx.data")
    flight, _ = _flight(tmp_path, server)
    sentences.append(download.public_state(flight.start(wait=True))["reason"])
    flight, _ = _flight(tmp_path, server, free_bytes=lambda _path: 1)
    sentences.append(download.public_state(flight.start(wait=True))["reason"])
    monkeypatch.setattr(download, "PINNED_MANIFESTS", {"v1": "0" * 64})
    flight, _ = _flight(tmp_path, server)
    sentences.append(download.public_state(flight.start(wait=True))["reason"])
    assert len(sentences) == 3 and all(sentences)
    assert not any("classifier" in text.lower() or "correction" in text.lower() for text in sentences)


def test_setup_marks_the_request_until_the_asset_lands_and_restarts_resume_it(tmp_path, server, pinned):
    marker = tmp_path / ".asset.requested"
    woken: list[None] = []
    server.corrupt.add("model.onnx.data")
    flight, _ = _flight(tmp_path, server, woken=woken)
    assert flight.start(wait=True)["state"] == "failed"
    assert marker.is_file() and woken == []

    server.corrupt.clear()
    restarted, _ = _flight(tmp_path, server, woken=woken)  # a new process: nothing in memory
    restarted.resume()
    restarted._thread.join()
    assert restarted.status() == {"state": "ready"}
    assert not marker.exists() and woken == [None]


def test_a_restart_with_no_request_downloads_nothing(tmp_path, server, pinned):
    flight, _ = _flight(tmp_path, server)
    flight.resume()
    assert flight.status() == {"state": "idle"} and flight._thread is None
    assert server.requests == []


def test_start_is_idempotent_while_running_and_after_ready(tmp_path, server, pinned):
    gate = threading.Event()
    entered = threading.Event()

    def blocking(_path: Path) -> None:
        entered.set()
        gate.wait(5)

    flight, _ = _flight(tmp_path, server, self_test=blocking)
    first = flight.start()
    second = flight.start()
    assert first["state"] in {"downloading", "self_testing", "verifying"}
    assert second["state"] in {"downloading", "self_testing", "verifying"}
    # The already-installed probe is not running: the asset is absent, so the one thread downloads.
    assert entered.wait(5)
    gate.set()
    flight._thread.join(5)

    assert flight.status() == {"state": "ready"}
    fetched = len(server.requests)
    assert flight.start() == {"state": "ready"}
    assert len(server.requests) == fetched
    assert server.requested("manifest.json") == [None]


def test_retry_after_failure_succeeds(tmp_path, server, pinned):
    server.corrupt.add("model.onnx.data")
    flight, _ = _flight(tmp_path, server)
    assert flight.start(wait=True)["state"] == "failed"

    server.corrupt.clear()
    assert flight.start(wait=True) == {"state": "ready"}


def test_already_installed_asset_that_self_tests_is_not_downloaded_again(tmp_path, server, assets, pinned):
    root = tmp_path / "asset"
    root.mkdir()
    for name, data in assets.items():
        (root / name).write_bytes(data)
    flight, tested = _flight(tmp_path, server)

    assert flight.start(wait=True) == {"state": "ready"}
    assert server.requests == [] and tested == [root]


def test_network_loss_gives_up_with_a_retryable_failure(tmp_path, assets, pinned):
    dead = _Server(assets)
    dead.close()
    flight, _ = _flight(tmp_path, dead)

    state = flight.start(wait=True)

    assert state["state"] == "failed" and state["code"] == "network_lost" and state["retryable"] is True


def test_only_the_flight_a_start_begins_reports_its_failure_once(tmp_path, assets, pinned):
    dead = _Server(assets)
    dead.close()
    flight, _ = _flight(tmp_path, dead)
    failures: list[dict] = []

    flight.start(wait=True, on_failure=failures.append)

    assert [state["code"] for state in failures] == ["network_lost"]
    flight.start(wait=True)  # a retry that names no listener reports to nobody
    assert len(failures) == 1


def test_a_successful_flight_reports_no_failure(tmp_path, server, pinned):
    flight, _ = _flight(tmp_path, server)
    failures: list[dict] = []

    assert flight.start(wait=True, on_failure=failures.append) == {"state": "ready"}
    assert failures == []


def test_the_release_location_follows_the_newest_pin_and_the_override_wins(monkeypatch):
    monkeypatch.delenv(download.BASE_URL_ENV, raising=False)
    monkeypatch.setattr(corrections_asset_files, "PINNED_MANIFESTS", {"v1": "a", "v2": "b"})

    assert download.base_url().endswith("/asset-corrections-classifier-v2")
    monkeypatch.setenv(download.BASE_URL_ENV, "http://127.0.0.1:9/")
    assert download.base_url() == "http://127.0.0.1:9"


def test_public_state_hides_internal_fields():
    assert download.public_state({"state": "downloading", "bytes_done": 3, "bytes_total": 9, "x": 1}) == {
        "state": "downloading", "bytes_done": 3, "bytes_total": 9,
    }
    assert download.public_state({"state": "idle"}) == {"state": "idle"}


# -- the setup routes -------------------------------------------------------------

LOOPBACK_BASE = "http:" + f"//127.0.0.1:{DEFAULT_BACKEND_PORT}"
LOOPBACK_HEADERS = {"Host": f"127.0.0.1:{DEFAULT_BACKEND_PORT}"}
JSON_HEADERS = {**LOOPBACK_HEADERS, "Content-Type": "application/json"}


@pytest.fixture
def api_client(tmp_path, server, pinned, monkeypatch):
    from api import setup as setup_api

    flight, _ = _flight(tmp_path, server)
    monkeypatch.setattr(setup_api, "CLASSIFIER_DOWNLOAD", flight)
    app = FastAPI()
    app.include_router(setup_api.router)
    with TestClient(app, base_url=LOOPBACK_BASE, client=("127.0.0.1", 50100)) as client:
        client.flight = flight
        yield client


def test_routes_refuse_a_non_loopback_host(api_client):
    wrong = {"Host": f"example.com:{DEFAULT_BACKEND_PORT}"}
    assert api_client.get("/api/setup/classifier", headers=wrong).status_code == 403
    assert api_client.post(
        "/api/setup/classifier", json={}, headers={**wrong, "Content-Type": "application/json"},
    ).status_code == 403


def test_the_focus_recheck_route_is_loopback_only_and_reports_the_state(api_client):
    wrong = {"Host": f"example.com:{DEFAULT_BACKEND_PORT}", "Content-Type": "application/json"}
    assert api_client.post("/api/setup/classifier/recheck", json={}, headers=wrong).status_code == 403
    assert api_client.post("/api/setup/classifier/recheck", json={}, headers=JSON_HEADERS).json() == {"state": "idle"}


def test_routes_start_and_report_state_and_take_no_path(api_client):
    assert api_client.get("/api/setup/classifier", headers=LOOPBACK_HEADERS).json() == {"state": "idle"}

    started = api_client.post("/api/setup/classifier", json={"url": "http://evil", "path": "/tmp/x"}, headers=JSON_HEADERS)
    assert started.status_code == 200
    api_client.flight._thread.join(10)

    assert api_client.get("/api/setup/classifier", headers=LOOPBACK_HEADERS).json() == {"state": "ready"}
    again = api_client.post("/api/setup/classifier", json={}, headers=JSON_HEADERS)
    assert again.json() == {"state": "ready"}


def test_the_setup_start_route_reports_a_failure_like_the_startup_start(api_client, monkeypatch):
    import app_startup_runtime

    calls = []
    monkeypatch.setattr(api_client.flight, "start", lambda **kwargs: calls.append(kwargs) or {"state": "idle"})
    api_client.post("/api/setup/classifier", json={}, headers=JSON_HEADERS)
    assert calls == [{"on_failure": app_startup_runtime.report_classifier_download_failure}]


def test_an_installed_asset_wakes_the_correction_worker_on_the_app_loop(monkeypatch):
    import asyncio

    import infra.app_loop as app_loop

    scans: list[str] = []
    monkeypatch.setattr(signals, "_waker", lambda: scans.append("scan"))
    monkeypatch.setattr(app_loop, "submit", lambda coroutine: asyncio.run(coroutine))

    signals.wake_corrections()

    assert scans == ["scan"]


def test_waking_the_worker_without_a_loop_is_not_an_error(monkeypatch):
    import infra.app_loop as app_loop

    def unavailable(coroutine):
        coroutine.close()
        raise app_loop.AppLoopUnavailable("no loop")

    monkeypatch.setattr(signals, "_waker", lambda: None)
    monkeypatch.setattr(app_loop, "submit", unavailable)

    signals.wake_corrections()
