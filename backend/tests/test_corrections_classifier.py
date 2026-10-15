from __future__ import annotations

import hashlib
import json

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


def test_wrong_manifest_pin_is_refused(tmp_path):
    from core.corrections_classifier import AssetError, AssetManifest

    with pytest.raises(AssetError, match="manifest sha256"):
        AssetManifest.load(_manifest(tmp_path), expected_sha256="0" * 64)


def test_wrong_input_policy_is_refused(tmp_path):
    from core.corrections_classifier import AssetError, AssetManifest

    with pytest.raises(AssetError, match="supported corrections asset"):
        AssetManifest.load(_manifest(tmp_path, policy="other"))
