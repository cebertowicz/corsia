from __future__ import annotations

import hashlib
import json
import socket

import pytest


def _manifest(root, *, policy="modernbert-owner-context-v2-2048"):
    model = root / "model.onnx"
    tokenizer = root / "tokenizer.json"
    model.write_bytes(b"model")
    tokenizer.write_bytes(b"tokenizer")
    files = {
        path.name: {"bytes": path.stat().st_size, "sha256": hashlib.sha256(path.read_bytes()).hexdigest()}
        for path in (model, tokenizer)
    }
    value = {
        "asset_id": "corrections-classifier", "version": "v1", "files": files,
        "heads": [{"name": "correction", "threshold": 0.595681, "labels": ["not_correction", "correction"]}],
        "input_policy_id": policy,
        "self_test": [{"owner": f"synthetic {index}", "expected_score": 0.5} for index in range(4)],
    }
    path = root / "manifest.json"
    path.write_text(json.dumps(value), encoding="utf-8")
    return path


def test_training_format_keeps_context_before_the_owner_message():
    from core.corrections_classifier import build_text

    assert build_text([("assistant", "I shipped it."), ("user", "Earlier.")], "That is wrong.") == (
        "[assistant] I shipped it.\n[user] Earlier.\n[OWNER] That is wrong."
    )


def test_verified_manifest_builds_the_named_correction_head(tmp_path):
    from core.corrections_classifier import AssetManifest

    path = _manifest(tmp_path)
    manifest = AssetManifest.load(path, expected_sha256=hashlib.sha256(path.read_bytes()).hexdigest())
    manifest.verify_files()
    version = manifest.checker_version()

    assert (version.asset_id, version.version, version.head, version.threshold) == (
        "corrections-classifier", "v1", "correction", 0.595681,
    )


def test_tampered_asset_file_is_refused(tmp_path):
    from core.corrections_classifier import AssetError, AssetManifest

    path = _manifest(tmp_path)
    manifest = AssetManifest.load(path)
    (tmp_path / "model.onnx").write_bytes(b"other")

    with pytest.raises(AssetError, match="sha256"):
        manifest.verify_files()


def test_verified_files_are_hashed_once_until_they_change(monkeypatch, tmp_path):
    import os

    from core import corrections_classifier
    from core.corrections_classifier import AssetError, AssetManifest

    path = _manifest(tmp_path)
    manifest = AssetManifest.load(path)
    hashes = []
    real_sha256 = corrections_classifier.hashlib.sha256
    monkeypatch.setattr(corrections_classifier.hashlib, "sha256", lambda *a: hashes.append(1) or real_sha256(*a))

    manifest.verify_files()
    first = len(hashes)
    manifest.verify_files()
    assert first == 2 and len(hashes) == first

    model = tmp_path / "model.onnx"
    model.write_bytes(b"MODEL")
    stat = model.stat()
    os.utime(model, ns=(stat.st_atime_ns, stat.st_mtime_ns + 1_000_000))
    with pytest.raises(AssetError, match="sha256"):
        manifest.verify_files()


def test_wrong_manifest_pin_is_refused(tmp_path):
    from core.corrections_classifier import AssetError, AssetManifest

    with pytest.raises(AssetError, match="manifest sha256"):
        AssetManifest.load(_manifest(tmp_path), expected_sha256="0" * 64)


def test_wrong_input_policy_is_refused(tmp_path):
    from core.corrections_classifier import AssetError, AssetManifest

    with pytest.raises(AssetError, match="supported corrections asset"):
        AssetManifest.load(_manifest(tmp_path, policy="other"))


def test_child_prohibits_outbound_connections_and_enables_ort_telemetry_opt_out(monkeypatch, tmp_path):
    from core.corrections_classifier import _disable_child_network

    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    monkeypatch.setenv("CORSIA_STATE_DIR", str(tmp_path / "state"))

    original_connect = socket.socket.connect
    try:
        _disable_child_network()
        with pytest.raises(OSError, match="prohibits outbound"):
            socket.socket(socket.AF_INET, socket.SOCK_STREAM).connect(("127.0.0.1", 9))
        assert not list(tmp_path.rglob("*device*"))
    finally:
        setattr(socket.socket, "connect", original_connect)


def test_child_self_test_mismatch_refuses_to_serve(monkeypatch, tmp_path):
    import sys
    from types import SimpleNamespace

    from core.corrections_classifier import _child_main

    path = _manifest(tmp_path)
    value = json.loads(path.read_text(encoding="utf-8"))
    value["self_test"][0]["expected_score"] = 0.9
    path.write_text(json.dumps(value), encoding="utf-8")

    tokenizer = SimpleNamespace(
        from_file=lambda _path: SimpleNamespace(
            encode=lambda _text, add_special_tokens=False: SimpleNamespace(ids=[1, 2]),
        ),
    )
    class Exp(list):
        def sum(self):
            return sum(self)

    numpy = SimpleNamespace(
        int64=object(), asarray=lambda value, dtype: value,
        exp=lambda value: Exp([1.0, 1.0]) if isinstance(value, list) else 1.0,
    )
    onnx = SimpleNamespace(
        disable_telemetry_events=lambda: None,
        SessionOptions=lambda: SimpleNamespace(),
        ExecutionMode=SimpleNamespace(ORT_SEQUENTIAL="sequential"),
        InferenceSession=lambda *_args, **_kwargs: SimpleNamespace(run=lambda *_a, **_kw: [[[0.0, 0.0]]]),
    )
    monkeypatch.setitem(sys.modules, "tokenizers", SimpleNamespace(Tokenizer=tokenizer))
    monkeypatch.setitem(sys.modules, "numpy", numpy)
    monkeypatch.setitem(sys.modules, "onnxruntime", onnx)
    sent = []
    connection = SimpleNamespace(send=sent.append, poll=lambda _timeout: False)
    original_connect = socket.socket.connect
    try:
        _child_main(connection, str(path))
    finally:
        setattr(socket.socket, "connect", original_connect)

    assert sent == [{"error": "error:AssetError"}]


class _FakeSession:
    instances: list = []

    def __init__(self, logits=(0.0, 0.0)):
        self.logits = logits
        _FakeSession.instances.append(self)

    def run(self, *_args, **_kwargs):
        return [[list(self.logits)]]


@pytest.fixture
def self_test_asset(monkeypatch, tmp_path):
    """A pinned four-vector asset under a fake ORT and tokenizer; scores are 0.5."""
    import numpy
    import sys
    from types import SimpleNamespace

    from core import corrections_asset_pins

    root = tmp_path / "v1"
    root.mkdir()
    path = _manifest(root)
    monkeypatch.setitem(corrections_asset_pins.PINNED_MANIFESTS, "v1", hashlib.sha256(path.read_bytes()).hexdigest())
    _FakeSession.instances = []
    onnx = SimpleNamespace(
        disable_telemetry_events=lambda: None,
        SessionOptions=lambda: SimpleNamespace(),
        ExecutionMode=SimpleNamespace(ORT_SEQUENTIAL="sequential"),
        InferenceSession=lambda *_a, **_kw: _FakeSession(),
    )
    tokenizer = SimpleNamespace(from_file=lambda _path: SimpleNamespace(
        encode=lambda _text, add_special_tokens=False: SimpleNamespace(ids=[1, 2]),
    ))
    monkeypatch.setitem(sys.modules, "onnxruntime", onnx)
    monkeypatch.setitem(sys.modules, "tokenizers", SimpleNamespace(Tokenizer=tokenizer))
    monkeypatch.setitem(sys.modules, "numpy", numpy)
    return root


def _rewrite_manifest(root, mutate):
    from core import corrections_asset_pins

    path = root / "manifest.json"
    value = json.loads(path.read_text(encoding="utf-8"))
    mutate(value)
    path.write_text(json.dumps(value), encoding="utf-8")
    corrections_asset_pins.PINNED_MANIFESTS["v1"] = hashlib.sha256(path.read_bytes()).hexdigest()


def test_self_test_passes_and_releases_its_session(self_test_asset):
    import gc
    import weakref

    from core.corrections_classifier import run_self_test

    assert run_self_test(self_test_asset) is None
    assert len(_FakeSession.instances) == 1
    reference = weakref.ref(_FakeSession.instances.pop())
    gc.collect()
    assert reference() is None


def test_self_test_refuses_a_second_session_while_one_is_running(self_test_asset):
    from core import corrections_classifier
    from core.corrections_classifier import AssetError, run_self_test

    assert corrections_classifier._SELF_TEST_LOCK.acquire(blocking=False)
    try:
        with pytest.raises(AssetError, match="already being checked"):
            run_self_test(self_test_asset)
    finally:
        corrections_classifier._SELF_TEST_LOCK.release()
    assert _FakeSession.instances == []
    run_self_test(self_test_asset)


def test_self_test_wrong_size_says_the_download_is_incomplete(self_test_asset):
    from core.corrections_classifier import AssetError, run_self_test

    (self_test_asset / "model.onnx").write_bytes(b"too short")
    with pytest.raises(AssetError) as raised:
        run_self_test(self_test_asset)
    assert str(raised.value) == (
        "The corrections classifier download is incomplete (the model file is missing or has the "
        "wrong size). Retry setup."
    )
    assert _FakeSession.instances == []


def test_self_test_sha_mismatch_says_the_download_is_damaged(self_test_asset):
    from core.corrections_classifier import AssetError, run_self_test

    (self_test_asset / "model.onnx").write_bytes(b"MODEL")
    with pytest.raises(AssetError) as raised:
        run_self_test(self_test_asset)
    assert "damaged (the model file does not match its checksum)" in str(raised.value)


def test_self_test_unpinned_manifest_is_refused(self_test_asset):
    from core import corrections_asset_pins
    from core.corrections_classifier import AssetError, run_self_test

    del corrections_asset_pins.PINNED_MANIFESTS["v1"]
    with pytest.raises(AssetError, match="does not recognise"):
        run_self_test(self_test_asset)


def test_self_test_passes_in_the_installed_location_whatever_it_is_named(self_test_asset):
    from core.corrections_classifier import run_self_test

    installed = self_test_asset.rename(self_test_asset.parent / "corrections-classifier")
    assert run_self_test(installed) is None


def test_runtime_refuses_an_unpinned_asset_in_the_installed_location(monkeypatch, tmp_path):
    from core import corrections_asset_pins
    from core.corrections_classifier import AssetError
    from jobs import corrections_realtime

    root = tmp_path / "corrections-classifier"
    root.mkdir()
    path = _manifest(root)
    monkeypatch.setattr(corrections_realtime, "asset_root", lambda: root)
    monkeypatch.setitem(corrections_asset_pins.PINNED_MANIFESTS, "v1", "0" * 64)
    with pytest.raises(AssetError, match="sha256 does not match"):
        corrections_realtime._manifest()
    monkeypatch.delitem(corrections_asset_pins.PINNED_MANIFESTS, "v1")
    with pytest.raises(AssetError, match="not pinned"):
        corrections_realtime._manifest()
    monkeypatch.setitem(
        corrections_asset_pins.PINNED_MANIFESTS, "v1", hashlib.sha256(path.read_bytes()).hexdigest(),
    )
    assert corrections_realtime._manifest().version == "v1"


def test_self_test_wrong_pin_hash_is_refused(self_test_asset):
    from core import corrections_asset_pins
    from core.corrections_classifier import AssetError, run_self_test

    corrections_asset_pins.PINNED_MANIFESTS["v1"] = "0" * 64
    with pytest.raises(AssetError, match="not the version this app expects"):
        run_self_test(self_test_asset)


def test_self_test_score_mismatch_says_the_answers_were_unexpected(self_test_asset):
    from core.corrections_classifier import AssetError, run_self_test

    _rewrite_manifest(self_test_asset, lambda value: value["self_test"][2].update(expected_score=0.9))
    with pytest.raises(AssetError, match="unexpected answers"):
        run_self_test(self_test_asset)
    assert len(_FakeSession.instances) == 1


def test_self_test_errors_never_expose_exception_text_or_type_names(self_test_asset, monkeypatch):
    import sys

    from core.corrections_classifier import AssetError, run_self_test

    def explode(*_args, **_kwargs):
        raise ValueError("secret detail")

    monkeypatch.setattr(sys.modules["onnxruntime"], "InferenceSession", explode)
    with pytest.raises(AssetError) as raised:
        run_self_test(self_test_asset)
    assert str(raised.value) == "The corrections classifier could not start on this computer. Retry setup."
    assert "secret" not in str(raised.value) and "ValueError" not in str(raised.value)
    assert raised.value.__cause__ is None and raised.value.__suppress_context__


def test_missing_manifest_is_a_readable_incomplete_download(tmp_path):
    from core.corrections_classifier import AssetError, run_self_test

    with pytest.raises(AssetError, match="incomplete"):
        run_self_test(tmp_path)
