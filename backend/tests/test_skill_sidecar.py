"""The ``corsia.yaml`` sidecar: pointer rules, bundle and file validation."""

from __future__ import annotations

import os

import pytest
import yaml

from core.skill_validation import validate_skill_bundle, validate_skill_file, validate_skill_text
from tests.skill_sidecar_helpers import POINTER

_WORKFLOW = {
    "handler": {"kind": "instruction"},
    "workflow": {"steps": [{"id": "build", "required": True, "check": "Build passes"}]},
}
_SIDECAR = yaml.safe_dump(_WORKFLOW, sort_keys=False)


def _skill(pointer: str = POINTER, block: str = "") -> str:
    return f"---\nname: sample\ndescription: Sample skill\n{pointer}{block}---\n# Sample\n"


def _legacy_block() -> str:
    return "corsia:\n" + "".join(f"  {line}\n" for line in _SIDECAR.splitlines())


def _lesson_block() -> str:
    return "corsia:\n  handler: {kind: cli}\n  lesson: {state: trial}\n"


def test_pointer_reads_the_sidecar_unwrapped():
    result = validate_skill_bundle(_skill(), {"corsia.yaml": _SIDECAR})

    assert result.ok, result.errors
    assert result.corsia == _WORKFLOW
    assert "corsia" not in result.metadata


@pytest.mark.parametrize(
    "value", ["../corsia.yaml", "/etc/corsia.yaml", "sub/corsia.yaml", "other.yaml", "''"],
)
def test_pointer_accepts_only_the_literal_file_name(value):
    pointer = f"metadata:\n  corsia-data: {value}\n  corsia-format: '1'\n"

    result = validate_skill_bundle(_skill(pointer), {"corsia.yaml": _SIDECAR, "other.yaml": _SIDECAR})

    assert not result.ok
    assert "metadata.corsia-data must be exactly corsia.yaml" in result.errors


@pytest.mark.parametrize("format_line", ["  corsia-format: '2'\n", "  corsia-format: 1\n", ""])
def test_unknown_or_missing_format_names_the_supported_version(format_line):
    pointer = f"metadata:\n  corsia-data: corsia.yaml\n{format_line}"

    result = validate_skill_bundle(_skill(pointer), {"corsia.yaml": _SIDECAR})

    assert not result.ok
    assert any("corsia-format must be '1', the supported version" in e for e in result.errors)


def test_pointer_to_a_missing_sidecar_is_an_error_not_empty():
    result = validate_skill_bundle(_skill(), None)

    assert not result.ok
    assert any("names corsia.yaml, which is not in the skill folder" in e for e in result.errors)
    assert result.corsia is None


@pytest.mark.parametrize("text", ["- a\n- b\n", "plain", ""])
def test_sidecar_must_be_a_mapping(text):
    result = validate_skill_bundle(_skill(), {"corsia.yaml": text})

    assert not result.ok
    assert "corsia.yaml must be a mapping" in result.errors


def test_sidecar_that_does_not_parse_is_an_error():
    result = validate_skill_bundle(_skill(), {"corsia.yaml": "a: [unclosed"})

    assert not result.ok
    assert any(e.startswith("corsia.yaml parse failed") for e in result.errors)


@pytest.mark.parametrize("key", ["lesson", "capture"])
def test_lesson_and_capture_keys_are_refused_in_a_sidecar(key):
    result = validate_skill_bundle(_skill(), {"corsia.yaml": f"{key}: {{state: trial}}\n"})

    assert not result.ok
    assert f"{key} is not allowed in corsia.yaml; it stays in the SKILL.md corsia block" in result.errors


def test_sidecar_data_gets_the_existing_handler_and_workflow_checks():
    bad = "handler: {kind: nope}\nworkflow: {steps: [{id: a, after: [missing]}]}\n"

    result = validate_skill_bundle(_skill(), {"corsia.yaml": bad})

    assert any("corsia.handler.kind must be one of" in e for e in result.errors)
    assert any("references unknown step" in e for e in result.errors)


def test_a_block_without_a_pointer_is_retired_and_names_the_sidecar():
    text = _skill(pointer="", block=_legacy_block())

    for result in (validate_skill_bundle(text, None), validate_skill_text(text)):
        assert not result.ok
        assert result.corsia is None
        assert [e for e in result.errors if "is retired" in e and "corsia.yaml" in e and "corsia-data" in e]


def test_a_lesson_block_still_resolves_as_the_skills_data_without_a_sidecar():
    text = _skill(pointer="", block=_lesson_block())

    for result in (validate_skill_bundle(text, None), validate_skill_text(text)):
        assert not any("retired" in e or "pointer" in e for e in result.errors), result.errors
        assert result.corsia == {"handler": {"kind": "cli"}, "lesson": {"state": "trial"}}
        assert result.corsia == result.metadata["corsia"]


def test_a_skill_with_no_corsia_data_has_none():
    assert validate_skill_bundle(_skill(pointer=""), None).corsia is None
    assert validate_skill_bundle(_skill(pointer=""), {"corsia.yaml": _SIDECAR}).corsia is None


@pytest.mark.parametrize("block", [_legacy_block(), "corsia:\n  handler: {kind: cli}\n"])
def test_block_and_pointer_together_are_always_an_error_even_with_identical_data(block):
    result = validate_skill_bundle(_skill(block=block), {"corsia.yaml": _SIDECAR})

    assert not result.ok
    assert any("a top-level corsia block and a metadata.corsia-data pointer both declare" in e for e in result.errors)
    assert validate_skill_text(_skill(block=block)).errors == [
        e for e in result.errors if "pointer both declare" in e
    ]


def test_a_lesson_block_and_a_pointer_together_are_an_error():
    result = validate_skill_bundle(_skill(block=_lesson_block()), {"corsia.yaml": _SIDECAR})

    assert not result.ok
    assert any("holding lesson data cannot also have a metadata.corsia-data pointer" in e for e in result.errors)


def test_validate_skill_text_never_consults_a_sidecar():
    result = validate_skill_text(_skill())

    assert result.ok
    assert result.corsia is None


def _folder(tmp_path):
    folder = tmp_path / "skills" / "sample"
    folder.mkdir(parents=True)
    (folder / "SKILL.md").write_text(_skill(), encoding="utf-8")
    return folder


def test_validate_skill_file_reads_the_sidecar_beside_it(tmp_path):
    folder = _folder(tmp_path)
    (folder / "corsia.yaml").write_text(_SIDECAR, encoding="utf-8")

    result = validate_skill_file(folder / "SKILL.md")

    assert result.ok, result.errors
    assert result.corsia == _WORKFLOW


def test_validate_skill_file_reports_a_missing_sidecar(tmp_path):
    result = validate_skill_file(_folder(tmp_path) / "SKILL.md")

    assert not result.ok
    assert any("not in the skill folder" in e for e in result.errors)


def test_validate_skill_file_refuses_a_symlinked_sidecar(tmp_path):
    folder = _folder(tmp_path)
    target = tmp_path / "elsewhere.yaml"
    target.write_text(_SIDECAR, encoding="utf-8")
    os.symlink(target, folder / "corsia.yaml")

    result = validate_skill_file(folder / "SKILL.md")

    assert not result.ok
    assert any("must be a regular file" in e for e in result.errors)
    assert result.corsia is None


def test_validate_skill_file_refuses_a_directory_sidecar(tmp_path):
    folder = _folder(tmp_path)
    (folder / "corsia.yaml").mkdir()

    assert any("must be a regular file" in e for e in validate_skill_file(folder / "SKILL.md").errors)


def test_a_symlinked_sidecar_is_ignored_when_nothing_points_at_it(tmp_path):
    folder = _folder(tmp_path)
    (folder / "SKILL.md").write_text(_skill(pointer=""), encoding="utf-8")
    os.symlink(tmp_path, folder / "corsia.yaml")

    assert validate_skill_file(folder / "SKILL.md").ok


def test_installed_digest_excludes_origin_but_includes_other_sidecar_data():
    from core.skill_sidecar import installed_digest

    files = {
        "SKILL.md": b"skill",
        "corsia.yaml": b"origin:\n  source: library:sample\n  adopted_at: now\nworkflow:\n  steps: []\n",
        "references/use.md": b"use it",
    }
    same_content_new_origin = {
        **files,
        "corsia.yaml": b"origin:\n  source: library:sample\n  adopted_at: later\nworkflow:\n  steps: []\n",
    }
    changed_workflow = {
        **files,
        "corsia.yaml": b"origin:\n  source: library:sample\nworkflow:\n  steps:\n    - id: build\n",
    }

    assert installed_digest(files) == installed_digest(same_content_new_origin)
    assert installed_digest(files) != installed_digest(changed_workflow)
