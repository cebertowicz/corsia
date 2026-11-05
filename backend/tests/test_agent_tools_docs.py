"""The generated agent-tools page follows the product catalog."""
from dataclasses import replace
from pathlib import Path
import subprocess
import sys

import pytest

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "scripts/docs"))

import generate_agent_tools as generator  # noqa: E402


def test_generated_page_is_current():
    subprocess.run(
        [sys.executable, str(ROOT / "scripts/docs/generate_agent_tools.py"), "--check"],
        cwd=ROOT, check=True,
    )


def _core(*names):
    return tuple({"function": {"name": name, "description": "Description. More."}}
                 for name in names)


def test_core_section_lists_exactly_the_advertised_tools():
    page = generator.render(_core("tool_call", "not_a_catalog_tool"))
    core = page.split("## Core tools", 1)[1].split("\n## ", 1)[0]
    assert "| `tool_call` | Description. |" in core
    assert "not_a_catalog_tool" not in core
    assert core.count("| `") == 1


def test_catalog_summary_changes_page(monkeypatch):
    name = next(name for names in generator.pack_members().values() for name in names)
    before = generator.render(_core())
    entry = generator.TOOL_CATALOG[name]
    monkeypatch.setitem(generator.TOOL_CATALOG, name, replace(entry, summary="Changed summary"))
    after = generator.render(_core())
    assert before != after
    assert f"| `{name}` | Changed summary" in after


def test_missing_catalog_summary_fails(monkeypatch):
    name = next(name for names in generator.pack_members().values() for name in names)
    entry = generator.TOOL_CATALOG[name]
    monkeypatch.setitem(generator.TOOL_CATALOG, name, replace(entry, summary=""))
    with pytest.raises(ValueError, match=f"No catalog summary for pack tool: {name}"):
        generator.render(_core())
