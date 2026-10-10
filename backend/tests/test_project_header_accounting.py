"""The project header is an accounted row of its own, not bytes hidden in one blob (#1241)."""

from __future__ import annotations

from unittest.mock import patch

from core.context_builder import ContextBuilder
from core.native_system_context import _component_rows, _retoken_native_system_components
from core.project_header import split_project_header


def _render(tmp_path):
    project_dir = tmp_path / "projects" / "alpha"
    (project_dir / "context").mkdir(parents=True)
    (project_dir / "context" / "PROJECT.md").write_text("# Alpha\nDo the thing.")
    (project_dir / "context" / "COORDINATION-MODEL.md").write_text("# Coordination")
    builder = ContextBuilder(workspace=tmp_path)
    with patch(
        "core.project_context.resolve_project_paths",
        return_value=("alpha", project_dir),
    ):
        return builder, builder.render_system_prompt(project_id="proj-alpha")


def test_header_and_files_are_separate_components_with_unchanged_bytes(tmp_path):
    builder, render = _render(tmp_path)
    by_id = {component.component_id: component for component in render.components}

    header, files = by_id["project-header:proj-alpha"], by_id["project:proj-alpha"]
    assert header.content.endswith("# Project: alpha\nProject workspace: " + str(tmp_path / "projects" / "alpha"))
    assert files.content.startswith("\n\n## ")
    assert "Do the thing." in files.content
    # The split changes accounting only: the injected prompt is what it was.
    assert "".join(component.content for component in render.components) == render.content
    with patch(
        "core.project_context.resolve_project_paths",
        return_value=("alpha", tmp_path / "projects" / "alpha"),
    ):
        whole = builder._load_project_context("proj-alpha")
    assert render.content.endswith("\n\n---\n\n" + whole)


def test_every_system_byte_is_charged_to_a_row_including_the_header(tmp_path):
    _builder, render = _render(tmp_path)
    rows = _retoken_native_system_components(
        _component_rows(list(render.components)), system_prompt=render.content,
    )
    tokens = {row.source_id: row.prompt_tokens_override for row in rows}

    assert tokens["native:system:project-header:proj-alpha"] > 0
    assert tokens["native:system:project:proj-alpha"] > 0
    assert all(value is not None for value in tokens.values())


def test_split_reassembles_and_leaves_headerless_text_whole():
    rendered = "# Project: a\nProject workspace: /w\n\n## PROJECT.md\n\nbody\n\n## X.md\n\nmore"
    header, files = split_project_header(rendered)
    assert header == "# Project: a\nProject workspace: /w"
    assert header + files == rendered
    assert split_project_header("PROJECT") == ("PROJECT", "")
