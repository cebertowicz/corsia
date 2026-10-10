"""Tool descriptions and edit errors say what the tools actually do."""

import pytest

from tools import filesystem_edit
from tools.catalog import TOOL_CATALOG
from tools.delegation_model import DelegateSetModelTool
from tools.filesystem_edit import MultiEditTool
from tools.filesystem_gate import AuthorizedPath
from tools.shell import BashTool


def test_set_model_is_described_as_in_place_switch_keeping_history():
    description = DelegateSetModelTool().description
    assert "running thread" in description
    assert "keeps its history" in description
    assert "addressability" not in description
    assert "running thread" in TOOL_CATALOG["delegate_set_model"].summary


def test_bash_redirects_browser_work_to_browser_tools():
    assert "browser_*" in BashTool().description


@pytest.mark.asyncio
async def test_multi_edit_miss_returns_nearest_match_and_writes_nothing(
    tmp_path, monkeypatch
):
    target = tmp_path / "f.txt"
    original = "alpha\nbeta gamma\ndelta\n"
    target.write_text(original)

    async def authorize(**_kwargs):
        return AuthorizedPath(target, reviewed=False)

    monkeypatch.setattr(filesystem_edit, "_resolve_path_for_action", authorize)
    result = await MultiEditTool(workspace=tmp_path).execute(
        path=str(target),
        edits=[
            {"old_string": "alpha", "new_string": "ALPHA"},
            {"old_string": "beta gamma!\n", "new_string": "x"},
        ],
    )
    assert result.startswith("Error: edit 2 old_string not found")
    assert "Best match" in result and "beta gamma" in result
    assert "No changes were written" in result and "Re-read the file" in result
    assert target.read_text() == original


def test_large_file_miss_compares_only_candidate_windows(monkeypatch):
    lines = [f"    value_{i} = compute({i})\n" for i in range(4000)]
    content = "".join(lines)
    old = "".join(lines[2500:2560]).replace("    value_2530", "  value_2530")
    calls = []
    real_ratio = filesystem_edit.difflib.SequenceMatcher.ratio

    def counting_ratio(self):
        calls.append(len(self.b))
        return real_ratio(self)

    monkeypatch.setattr(filesystem_edit.difflib.SequenceMatcher, "ratio", counting_ratio)
    result = filesystem_edit._not_found_message(old, content, "big.py")

    assert "(actual, line 2501)" in result
    assert len(calls) <= filesystem_edit._MATCH_CANDIDATES
