"""Real ripgrep binary output must remain visible or be reported incomplete."""

import pytest

from tools.grep import GrepTool


@pytest.mark.parametrize("mode", ["files", "count", "content"])
async def test_binary_records_are_visible_or_incomplete(tmp_path, monkeypatch, mode):
    from tools import grep, project_fence, sandbox_config
    from tools.project_fence import Fence

    if not grep._RG_AVAILABLE:
        pytest.skip("ripgrep unavailable")
    monkeypatch.setattr(project_fence, "fence_for_thread", lambda _thread_id: Fence())
    monkeypatch.setattr(sandbox_config, "_STATIC_ALLOWED_RW", [tmp_path])
    monkeypatch.setattr(sandbox_config, "_STATIC_ALLOWED_RO", [])
    monkeypatch.setattr(sandbox_config, "_list_authoritative_repo_policies", lambda: [])
    binary = tmp_path / "binary.dat"
    binary.write_bytes(b"needle\n" + b"a" * 20000 + b"\0needle\n")
    result = await GrepTool(workspace=tmp_path).execute("needle", path=str(binary), output_mode=mode, _thread_id="caller")
    if mode == "content":
        assert "INCOMPLETE" in result
        assert "binary file matches" not in result
    else:
        assert ("binary.dat" in result) if mode == "files" else result.strip().isdecimal()
        assert "INCOMPLETE" not in result
    assert "Other projects this agent can't read were left out." not in result
