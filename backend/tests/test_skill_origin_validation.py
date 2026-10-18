from __future__ import annotations

import pytest

from core.skill_validation import validate_skill_bundle


SKILL = """---
name: imported
description: Use an imported skill.
metadata:
  corsia-data: corsia.yaml
  corsia-format: '1'
---
# Imported
"""
VALID_ORIGIN = """origin:
  source: claude-code:~/.claude/skills/imported
  sha256: 0123456789abcdef0123456789abcdef0123456789abcdef0123456789abcdef
  adopted_at: 2026-10-18T12:00:00-07:00
"""


def _validate(sidecar: str):
    return validate_skill_bundle(SKILL, {"corsia.yaml": sidecar})


def test_origin_only_sidecar_is_valid_and_is_returned_with_the_skill():
    result = _validate(VALID_ORIGIN)

    assert result.ok
    assert result.corsia["origin"]["source"] == "claude-code:~/.claude/skills/imported"
    assert result.corsia["origin"]["sha256"] == "0123456789abcdef0123456789abcdef0123456789abcdef0123456789abcdef"
    assert result.corsia["origin"]["adopted_at"].isoformat() == "2026-10-18T12:00:00-07:00"


def test_origin_version_is_optional():
    result = _validate(VALID_ORIGIN + "  version: 1.2.3\n")

    assert result.ok
    assert result.corsia["origin"]["version"] == "1.2.3"


@pytest.mark.parametrize(("suffix", "valid"), [
    ("", True),
    ("  installed_digest: " + "b" * 64 + "\n", True),
    ("  installed_digest: not-a-digest\n", False),
])
def test_installed_digest_is_optional_but_must_be_a_sha256(suffix: str, valid: bool):
    result = _validate(VALID_ORIGIN + suffix)

    assert result.ok is valid
    if not valid:
        assert "corsia.origin.installed_digest must be 64 hexadecimal characters" in result.errors


def test_invalid_origin_reaches_the_existing_not_loaded_error_block(tmp_path, monkeypatch):
    from core.lesson_index import LessonPartition
    from core.skill_registry_types import SkillResolution
    from core.skill_scanning import scan_scope
    import core.skills_registry as registry

    skill = tmp_path / "skills" / "imported"
    skill.mkdir(parents=True)
    (skill / "SKILL.md").write_text(SKILL, encoding="utf-8")
    (skill / "corsia.yaml").write_text(
        VALID_ORIGIN.replace("0123456789abcdef0123456789abcdef0123456789abcdef0123456789abcdef", "bad"),
        encoding="utf-8",
    )
    error = scan_scope(tmp_path, "workspace", "workspace")[0]
    monkeypatch.setattr(registry, "resolve_effective_skills", lambda **_kwargs: SkillResolution([], [], [error]))
    monkeypatch.setattr(registry, "partition", lambda *_args, **_kwargs: LessonPartition([], [], []))
    monkeypatch.setattr(registry, "emit_product_event", lambda *_args, **_kwargs: None)

    index = registry.skill_context_sources(
        thread_id="thread", project_id=None, workspace=tmp_path, db=None,
    )[0]

    assert "Not loaded — skill errors" in index.prompt_content
    assert "corsia.origin.sha256" in index.prompt_content


@pytest.mark.parametrize(("sidecar", "field"), [
    ("origin:\n  sha256: " + "a" * 64 + "\n  adopted_at: 2026-10-18T12:00:00Z\n", "source"),
    ("origin:\n  source: claude-code:~/.claude/skills/imported\n  adopted_at: 2026-10-18T12:00:00Z\n", "sha256"),
    ("origin:\n  source: claude-code:~/.claude/skills/imported\n  sha256: " + "a" * 64 + "\n", "adopted_at"),
    (VALID_ORIGIN.replace("0123456789abcdef0123456789abcdef0123456789abcdef0123456789abcdef", "not-a-digest"), "sha256"),
    (VALID_ORIGIN.replace("claude-code:~/.claude/skills/imported", "/Users/example/skill"), "source"),
])
def test_invalid_origin_fields_are_validation_errors(sidecar: str, field: str):
    result = _validate(sidecar)

    assert not result.ok
    assert any(f"corsia.origin.{field}" in error for error in result.errors)
