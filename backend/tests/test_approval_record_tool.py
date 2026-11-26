"""The read-only tool exposes the approved-version lookup."""

import json

import pytest

from tools.approval_record import ApprovalRecordTool


@pytest.mark.asyncio
async def test_read_mode_and_validation(isolated_db):
    tool = ApprovalRecordTool()
    assert await tool.execute() == 'Only mode "read" is available.'
    assert await tool.execute(mode="read", integration="github") == (
        "integration, type, and external_id are required strings."
    )
    result = json.loads(await tool.execute(mode="read", integration="github", type="pull_request", external_id="unknown"))
    assert result == {"read_status": "not registered", "tie_status": None, "owning_node": None, "artifacts": []}
