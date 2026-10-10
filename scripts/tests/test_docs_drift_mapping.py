"""Tests for starter-kit path-mapping resolution (check_docs_drift.starter_kit_mapping)."""

from __future__ import annotations

from pathlib import Path, PurePosixPath

from check_docs_drift import starter_kit_mapping as mapping


def test_context_files_map_to_workspace_root_context_not_project_context(tmp_path):
    """context/** sources from <workspace>/context/**, never the project tree."""
    resolution = mapping.resolve_source(PurePosixPath("context/PROJECT.md"), tmp_path)

    assert resolution.kind == "copy"
    assert resolution.source == tmp_path / "context" / "PROJECT.md"


def test_shared_standards_maps_into_the_project_scoped_tree(tmp_path):
    """shared/standards/** has no workspace-root counterpart; only the project one."""
    resolution = mapping.resolve_source(
        PurePosixPath("shared/standards/WORKSPACE-STANDARD.md"), tmp_path
    )

    assert resolution.kind == "copy"
    assert resolution.source == tmp_path / "projects" / "corsia" / "shared" / "standards" / "WORKSPACE-STANDARD.md"


def test_skill_resolves_to_global_skills_when_present_there(tmp_path):
    """A skill/** file that exists in the global skills/ dir resolves there first."""
    global_file = tmp_path / "skills" / "github" / "SKILL.md"
    global_file.parent.mkdir(parents=True)
    global_file.write_text("global", encoding="utf-8")

    resolution = mapping.resolve_source(PurePosixPath("skills/github/SKILL.md"), tmp_path)

    assert resolution.kind == "copy"
    assert resolution.source == global_file


def test_skill_falls_back_to_project_scoped_skills_when_global_is_absent(tmp_path):
    """The kit's design-interrogation skill lives only under the project tree."""
    project_file = (
        tmp_path / "projects" / "corsia" / "skills" / "design-interrogation" / "SKILL.md"
    )
    project_file.parent.mkdir(parents=True)
    project_file.write_text("project-scoped", encoding="utf-8")

    resolution = mapping.resolve_source(
        PurePosixPath("skills/design-interrogation/SKILL.md"), tmp_path
    )

    assert resolution.kind == "copy"
    assert resolution.source == project_file


def test_project_skill_default_kind_is_copy():
    resolution = mapping.resolve_source(
        PurePosixPath("project-skills/github/SKILL.md"), PurePosixPath("/ws")
    )

    assert resolution.kind == "copy"


def test_project_skill_in_the_adapted_set_is_marked_adapted_not_copy():
    """sdlc-process, ci-gates-locally, corsia-test-environment are rewritten, not scrubbed."""
    resolution = mapping.resolve_source(
        PurePosixPath("project-skills/sdlc-process/SKILL.md"), PurePosixPath("/ws")
    )

    assert resolution.kind == "adapted"


def test_kit_native_file_has_no_source_to_diff_against():
    resolution = mapping.resolve_source(
        PurePosixPath("templates/PROJECT.md"), PurePosixPath("/ws")
    )

    assert resolution.kind == "kit_native"
    assert resolution.source is None


def test_every_kit_native_entry_is_a_real_kit_file():
    """The kit-native set names the kit's own files, not deleted or packaged ones."""
    kit_root = Path(__file__).resolve().parents[2] / mapping.KIT_ROOT_REL
    for rel in sorted(mapping.KIT_NATIVE_FILES):
        assert (kit_root / rel).is_file(), f"kit-native entry missing from the kit: {rel}"


def test_unrecognized_top_level_directory_is_unmapped():
    resolution = mapping.resolve_source(PurePosixPath("nonsense/foo.md"), PurePosixPath("/ws"))

    assert resolution.kind == "unmapped"
    assert resolution.source is None
