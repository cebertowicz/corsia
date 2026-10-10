from __future__ import annotations

import pytest

from tools.catalog import TOOL_CATALOG, normalize_tool_name


@pytest.mark.parametrize("raw,canonical", [
    ("mcp__corsia__write_file", "write_file"),
    ("mcp__corsia__edit_file", "edit_file"),
    ("mcp__corsia__multi_edit", "multi_edit"),
    ("mcp__corsia__bash", "bash"),
    ("mcp__corsia__read_file", "read_file"),
    ("Write", "write_file"),
    ("Edit", "edit_file"),
    ("MultiEdit", "multi_edit"),
    ("Bash", "bash"),
    ("Read", "read_file"),
    ("write_file", "write_file"),
    ("edit_file", "edit_file"),
    ("multi_edit", "multi_edit"),
    ("bash", "bash"),
    ("read_file", "read_file"),
])
def test_normalize_tool_name_maps_every_harness_variant_to_canonical(raw, canonical):
    assert normalize_tool_name(raw) == canonical


def test_normalize_tool_name_passes_through_unknown_names():
    assert normalize_tool_name("NotebookEdit") == "NotebookEdit"
    assert normalize_tool_name("TodoWrite") == "TodoWrite"


def test_normalize_tool_name_handles_missing_name():
    assert normalize_tool_name(None) == ""
    assert normalize_tool_name("") == ""


def test_normalized_aliases_target_real_catalog_entries():
    from tools.catalog import CC_BUILTIN_ALIASES

    for canonical in CC_BUILTIN_ALIASES.values():
        assert canonical in TOOL_CATALOG
