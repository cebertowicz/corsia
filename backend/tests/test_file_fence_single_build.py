from unittest.mock import AsyncMock

import pytest


@pytest.mark.asyncio
@pytest.mark.parametrize('name', ['read_file', 'mcp__corsia__read_file', 'Read'])
async def test_registry_reuses_one_fence_for_file_action(tmp_path, monkeypatch, name):
    from tools import project_fence, recorded_call, sandbox_config
    from tools.filesystem_rw import ReadFileTool
    from tools.registry import ToolRegistry

    calls = []

    def build(thread_id):
        calls.append(thread_id)
        return project_fence.Fence(caller=thread_id)

    monkeypatch.setattr(project_fence, 'fence_for_thread', build)
    monkeypatch.setattr('tools.host_path_policy.fence_for_thread', build)
    monkeypatch.setattr(sandbox_config, '_STATIC_ALLOWED_RW', [tmp_path])
    monkeypatch.setattr(sandbox_config, '_STATIC_ALLOWED_RO', [])
    monkeypatch.setattr(sandbox_config, '_list_authoritative_repo_policies', lambda: [])
    monkeypatch.setattr(recorded_call, 'record_dispatch', AsyncMock(return_value=False))
    path = tmp_path / 'sample.txt'
    path.write_text('visible')
    tool = ReadFileTool(workspace=tmp_path)
    params = {'path': str(path)}
    context = {'id': 'caller', 'project_id': None}
    result = await ToolRegistry()._authorized_execute(
        tool, name, params, dict(params, _thread_id='caller', _thread_project_id=None), context,
    )
    assert 'visible' in result
    assert calls == ['caller']


@pytest.mark.asyncio
async def test_direct_screenshot_builds_fence_without_registry(tmp_path, monkeypatch):
    from tools import browser, host_access_gate, project_fence

    fenced = tmp_path / 'other'
    fenced.mkdir()
    fence = project_fence.Fence((project_fence.FencedRoot(fenced, project='other'),), caller='caller')
    monkeypatch.setattr(project_fence, 'fence_for_thread', lambda thread_id: fence)
    monkeypatch.setattr(project_fence, '_event', lambda *args, **kwargs: None)
    monkeypatch.setattr(host_access_gate, 'consume_current_action_permit', AsyncMock(return_value=None))
    result = await browser.BrowserScreenshotTool(workspace=tmp_path).execute(
        path=str(fenced / 'shot.png'), _thread_id='caller',
    )
    assert 'outside your scope' in result
