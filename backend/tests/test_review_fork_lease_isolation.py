"""A review fork cannot inherit its reviewer's host-access lease."""

import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from core.artifact_review_fork_kind import ARTIFACT_REVIEW_KIND
from core.fork_tools import execute_fork_tool
from test_doc_review_requests import request, review_env  # noqa: F401
from test_project_fence import world  # noqa: F401
from tools.filesystem_rw import ReadFileTool
from tools.registry import ToolRegistry


@pytest.mark.asyncio
async def test_review_read_does_not_use_reviewers_live_lease(review_env, world, monkeypatch, tmp_path):
    from tools import host_access_gate, sandbox_config

    db, aid = review_env
    row = request(db, aid)
    db.execute("UPDATE threads SET project_id='id-alpha' WHERE id='reviewer'")
    db.commit()
    ws = world.workspace
    outside = tmp_path / 'outside.txt'
    outside.write_text('outside content')
    monkeypatch.setattr(sandbox_config, 'WORKSPACE', ws)
    monkeypatch.setattr(sandbox_config, '_STATIC_ALLOWED_RW', [ws])
    monkeypatch.setattr(sandbox_config, '_STATIC_ALLOWED_RO', [])
    monkeypatch.setattr(sandbox_config, '_list_authoritative_repo_policies', lambda: [])
    resolve = AsyncMock(return_value={'requested_policy_exceptions': []})
    review = AsyncMock(side_effect=AssertionError('lease must not be reviewed'))
    monkeypatch.setattr(host_access_gate, '_resolve_active_lease', resolve)
    monkeypatch.setattr(host_access_gate, '_review_action', review)
    registry = ToolRegistry()
    registry.register(ReadFileTool(workspace=ws))
    provider = SimpleNamespace(fork_id=row['fork_id'], thread_id='reviewer', working_dir=str(ws),
                               target=SimpleNamespace(catalog_id='cloud/model'))

    result = await execute_fork_tool(ARTIFACT_REVIEW_KIND, provider, registry, asyncio.Event(),
                                     'read_file', {'path': str(outside)})
    assert result.startswith('Error')
    assert 'outside protected scope' in result
    assert 'outside content' not in result
    resolve.assert_not_awaited()
    review.assert_not_awaited()

    # The review's task-local restriction must not remove the reviewer's own lease.
    from infra import state
    token = state._current_thread_id.set('reviewer')
    try:
        ordinary = await registry.execute('read_file', {'path': str(outside)})
    finally:
        state._current_thread_id.reset(token)
    assert 'lease must not be reviewed' in ordinary
    resolve.assert_awaited_once_with('reviewer')
    review.assert_awaited_once()
