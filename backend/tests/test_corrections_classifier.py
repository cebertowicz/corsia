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
