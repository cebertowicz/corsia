from __future__ import annotations

import hashlib
import importlib.util
import json
import sys
from pathlib import Path

import pytest


ROOT = Path(__file__).resolve().parents[2]


def _load_generator():
    spec = importlib.util.spec_from_file_location(
        "generate_system_manifest", ROOT / "scripts/generate_system_manifest.py",
    )
    assert spec is not None and spec.loader is not None
    generator = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(generator)
    return generator


def test_committed_manifest_matches_generator_and_every_entry_exists_on_disk():
    """A context file added without regenerating the manifest must fail this test."""
    generator = _load_generator()
    committed = generator.MANIFEST.read_text(encoding="utf-8")
    rendered = json.dumps(generator.manifest_payload(), indent=2, sort_keys=True) + "\n"

    assert committed == rendered

    from infra.system_package import system_resource_source

    for item in json.loads(committed)["files"]:
        assert system_resource_source(item["path"]).is_file()


def test_every_manifest_file_loads_with_declared_digest_and_strict_utf8():
    from core.system_tier import _read, system_tier_manifest

    manifest = system_tier_manifest()

    assert manifest.version == "v1"
    assert manifest.mismatches == ()
    assert manifest.files
    for item in manifest.files:
        raw, content = _read(item.path)
        assert content == raw.decode("utf-8", errors="strict")
        assert hashlib.sha256(raw).hexdigest() == item.sha256
        assert len(raw) == item.byte_count


def test_manifest_generator_rejects_stale_manifest(tmp_path, monkeypatch):
    generator = _load_generator()
    stale_manifest = tmp_path / "MANIFEST.json"
    stale_manifest.write_text('{"version":"stale","files":[]}\n', encoding="utf-8")
    monkeypatch.setattr(generator, "MANIFEST", stale_manifest)
    monkeypatch.setattr(sys, "argv", ["generate_system_manifest.py", "--check"])

    assert generator.main() == 1


def test_edited_coordination_model_without_regeneration_fails_check(tmp_path, monkeypatch):
    """A docs-only edit must fail the freshness check that docs-drift.yml runs."""
    import shutil

    generator = _load_generator()
    models = tmp_path / "coordination-models"
    shutil.copytree(generator.coordination_models_dir(), models)
    monkeypatch.setattr(generator, "coordination_models_dir", lambda: models)
    monkeypatch.setattr(sys, "argv", ["generate_system_manifest.py", "--check"])
    assert generator.main() == 0

    index = models / "INDEX.md"
    index.write_text(index.read_text(encoding="utf-8") + "\nEdited.\n", encoding="utf-8")

    assert generator.main() == 1
    workflow = (ROOT / ".github/workflows/docs-drift.yml").read_text(encoding="utf-8")
    assert "python scripts/generate_system_manifest.py --check" in workflow


def test_unknown_resource_path_is_rejected():
    from core.system_tier import _read

    with pytest.raises(ValueError, match="invalid System manifest resource path"):
        _read("../unknown.md")


def test_manifest_mismatch_delivers_current_bytes_and_one_visible_error(tmp_path, monkeypatch, caplog):
    from core import system_tier

    manifest = system_tier.system_tier_manifest()
    item = next(entry for entry in manifest.files if entry.kind == "context")
    original_read = system_tier._read
    changed_raw = b"# changed\n"

    def changed_read(path: str):
        if path == item.path:
            return changed_raw, changed_raw.decode("utf-8")
        return original_read(path)

    monkeypatch.setattr(system_tier, "_read", changed_read)
    system_tier._WARNED_MISMATCHES.clear()

    first = system_tier.system_tier_sources((), workspace=tmp_path)
    second = system_tier.system_tier_sources((), workspace=tmp_path)
    delivered = next(row for row in first if row.metadata.get("resource") == item.path)
    errors = [row for row in first if row.source_id == "corsia:error:manifest-mismatch"]

    assert delivered.prompt_content == f"## {Path(item.path).name}\n\n# changed\n"
    assert delivered.metadata["raw_sha256"] == hashlib.sha256(changed_raw).hexdigest()
    assert len(errors) == 1
    assert item.path in errors[0].reason
    assert item.sha256 in errors[0].reason
    assert hashlib.sha256(changed_raw).hexdigest() in errors[0].reason
    assert "regenerate the manifest" in errors[0].reason
    assert len([row for row in second if row.source_id == "corsia:error:manifest-mismatch"]) == 1
    warnings = [record for record in caplog.records if "System manifest mismatch" in record.message]
    assert len(warnings) == 1
