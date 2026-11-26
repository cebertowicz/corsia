"""The read-only tool exposes the approved-version lookup."""

import json

import pytest

from tools.approval_record import ApprovalRecordTool
from tests.test_shared_document_ownership import ownership_scope as ownership_scope


@pytest.mark.asyncio
async def test_read_mode_and_validation(isolated_db):
    tool = ApprovalRecordTool()
    assert await tool.execute() == 'Only mode "read" is available.'
    assert await tool.execute(mode="read", integration="github") == (
        "integration, type, and external_id are required strings."
    )
    from infra.database import get_db
    from tests.registry_helpers import make_session
    db = get_db()
    try:
        thread = make_session(db, prefix="approval-tool")
        db.commit()
    finally:
        db.close()
    result = json.loads(await tool.execute(mode="read", integration="github", type="pull_request", external_id="unknown", _thread_id=thread))
    assert result == {"read_status": "not registered", "tie_status": None, "owning_node": None, "artifacts": []}


@pytest.mark.asyncio
async def test_script_channel_scopes_approved_documents(ownership_scope, monkeypatch):
    import asyncio
    from automation.script_tool_channel import _serve
    from plans import service as plans_service
    from plans.approval_records import record_approval
    from registry import service
    from test_script_github_proxy import Writer
    from tests.registry_helpers import make_session
    from tests.test_plan_linked_node_for_artifact import PR, _active_task, _registered_from
    from tools import sandbox_config
    from tools.registry import ToolRegistry

    scope = ownership_scope
    db = scope['db']
    beta = scope['workspace'] / 'projects' / 'beta'
    beta.mkdir(parents=True)
    db.execute("INSERT INTO projects (id,name,path,status,created_at,updated_at) VALUES (?,?,?,?,?,?)",
               ('project-beta', 'Beta', 'workspace://workspace-stable-id/projects/beta', 'active', 't', 't'))
    outsider = make_session(db, project_id='project-beta', prefix='outside')
    path = scope['root'] / '.threads' / scope['owner'] / 'review.md'
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text('approved')
    service.register_write_strict(scope['owner'], str(path), db=db)
    artifact = service.find_file_identity(str(path), db=db)
    db.commit()
    plan = plans_service.ensure_plan('thread', scope['owner'], db=db)
    node = _active_task(db, plan.id, 'Review work')
    _registered_from(db, scope['owner'], plan.id, node.id)
    record_approval(db, artifact_id=artifact['id'], node_id=node.id, source='bar', source_id='hold',
                    doc_sha256='a' * 64, images=[])
    db.commit()
    monkeypatch.setattr(sandbox_config, 'is_path_allowed', lambda *args, **kwargs: True)
    registry = ToolRegistry()
    registry.register(ApprovalRecordTool())

    async def call(thread):
        reader = asyncio.StreamReader()
        reader.feed_data(json.dumps({'name': 'approval_record', 'arguments': {
            'mode': 'read', 'integration': PR[0], 'type': PR[1], 'external_id': PR[2],
        }}).encode() + b'\n')
        reader.feed_eof()
        writer = Writer()
        await _serve(reader, writer, thread_id=thread, registry=registry)
        response = json.loads(bytes(writer.data[8:]))
        assert response['code'] == 0
        return json.loads(response['stdout'])

    assert len((await call(scope['other']))['artifacts']) == 1
    hidden = await call(outsider)
    assert hidden['artifacts'] == []
    assert hidden['tie_status'] == 'tied'  # withheld documents never read as "none tied"
