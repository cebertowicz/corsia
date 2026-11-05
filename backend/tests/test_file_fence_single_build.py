from unittest.mock import AsyncMock

import pytest


@pytest.mark.asyncio
async def test_registry_reuses_one_fence_for_file_action(tmp_path, monkeypatch):
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
        tool, 'read_file', params, dict(params, _thread_id='caller', _thread_project_id=None), context,
    )
    assert 'visible' in result
    assert calls == ['caller']
