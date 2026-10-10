"""Artifact tools read a relative path the way the agent's file tools do (#872).

A harness session's tool registry is built on the session working directory,
which is often a project or repository below the workspace root. The file and
shell tools join a relative path onto that directory, so the artifact tools
built in the same registry must name the same physical file.
"""

from __future__ import annotations

import json
import uuid
from pathlib import Path

import pytest

from delegation_docs_fixtures import _OWNER, _mk_thread, _registrable_roots

pytestmark = pytest.mark.integration


@pytest.fixture(autouse=True)
def _workspace_root_decoy(integration_workspace):
    """A file at the same relative path under the workspace root.

    A tool that joins onto the workspace root instead of its own working
    directory then names a real, different file rather than a missing one.
    """
    decoy = integration_workspace / "notes.md"
    decoy.write_text("workspace-root decoy")
    yield
    decoy.unlink(missing_ok=True)


def _session(workspace: Path, sid: str):
    """A harness working directory below the workspace root, and its tools."""
    from subagents.harness_tool_bridge import build_tool_registry

    session = workspace / ".threads" / sid / "session"
    session.mkdir(parents=True)
    return session.resolve(), build_tool_registry(str(session))


async def _write(tools, sid: str, path: str, content: str) -> None:
    result = await tools.get("write_file").execute(
        path=path, content=content, _thread_id=sid,
    )
    assert not result.startswith("Error"), result


@pytest.mark.asyncio
async def test_artifact_tools_name_the_file_write_file_wrote(
    integration_client,
    integration_workspace,
    _registrable_roots,
):
    sid = _mk_thread(integration_client, _OWNER, title="Harness session")
    session_dir, tools = _session(integration_workspace, sid)
    await _write(tools, sid, "notes.md", "written by the agent")
    written = session_dir / "notes.md"
    assert written.read_text() == "written by the agent"

    registered = json.loads(await tools.get("artifact_register").execute(
        path="notes.md", _thread_id=sid,
    ))["artifact"]
    resolved = json.loads(await tools.get("artifact_resolve").execute(
        path="notes.md", _thread_id=sid,
    ))
    listed = json.loads(await tools.get("doc_comments_list").execute(
        doc_path="notes.md", _thread_id=sid,
    ))

    assert registered["path"] == str(written)
    assert resolved["found"] is True
    assert resolved["artifact"]["id"] == registered["id"]
    assert listed == {
        "found": True, "artifact_id": registered["id"], "comments": [], "anchors_checked": True,
    }


@pytest.mark.asyncio
async def test_artifact_relocate_target_uses_the_session_directory(
    integration_client,
    integration_workspace,
    _registrable_roots,
):
    sid = _mk_thread(integration_client, _OWNER, title="Harness session")
    session_dir, tools = _session(integration_workspace, sid)
    await _write(tools, sid, "draft.md", "draft")
    registered = json.loads(await tools.get("artifact_register").execute(
        path="draft.md", _thread_id=sid,
    ))["artifact"]

    moved = json.loads(await tools.get("artifact_relocate").execute(
        artifact_id=registered["id"], target_path="final.md", _thread_id=sid,
    ))["artifact"]

    assert moved["path"] == str(session_dir / "final.md")
    assert (session_dir / "final.md").read_text() == "draft"


@pytest.mark.asyncio
async def test_relative_path_outside_registrable_roots_keeps_existing_answers(
    integration_client,
    integration_workspace,
    _registrable_roots,
):
    sid = _mk_thread(integration_client, _OWNER, title="Harness session")
    _session_dir, tools = _session(integration_workspace, sid)
    outside = "../../../../outside-registrable-roots.md"

    resolved = json.loads(await tools.get("artifact_resolve").execute(
        path=outside, _thread_id=sid,
    ))

    assert resolved == {"found": False, "artifact": None}
    with pytest.raises(ValueError, match="outside the registrable roots"):
        await tools.get("artifact_register").execute(path=outside, _thread_id=sid)


@pytest.mark.asyncio
async def test_unresolvable_project_scope_does_not_change_the_path_base(
    integration_client,
    integration_workspace,
    _registrable_roots,
):
    from infra.database import get_db, write_transaction

    project = integration_client.post(
        f"/api/projects?token={_OWNER}",
        json={"name": f"Unresolved artifact project {uuid.uuid4().hex[:6]}"},
    ).json()["project"]
    sid = integration_client.post(
        f"/api/threads?token={_OWNER}",
        json={"title": "Project-scoped session", "project_id": project["id"]},
    ).json()["thread_id"]
    db = get_db()
    try:
        with write_transaction(db, scope="test_unresolved_artifact_project"):
            db.execute(
                "UPDATE projects SET path=? WHERE id=?",
                (str(Path(project["path"]) / "missing-root"), project["id"]),
            )
    finally:
        db.close()
    session_dir, tools = _session(integration_workspace, sid)
    # Write governance refuses this thread's writes while its project root is
    # unavailable, so the file stands in for one written before the root moved.
    (session_dir / "notes.md").write_text("written before the root moved")

    registered = json.loads(await tools.get("artifact_register").execute(
        path="notes.md", _thread_id=sid,
    ))["artifact"]
    resolved = json.loads(await tools.get("artifact_resolve").execute(
        path="notes.md", _thread_id=sid,
    ))

    assert registered["path"] == str(session_dir / "notes.md")
    assert resolved["artifact"]["id"] == registered["id"]


@pytest.mark.asyncio
async def test_native_artifact_tools_keep_the_workspace_root_base(
    integration_client,
    integration_workspace,
    _registrable_roots,
):
    from tools.artifacts import ArtifactRegisterTool, ArtifactResolveTool

    sid = _mk_thread(integration_client, _OWNER, title="Native agent")
    name = f"native-{uuid.uuid4().hex[:8]}.md"
    (integration_workspace / name).write_text("native write")

    registered = json.loads(await ArtifactRegisterTool().execute(
        path=name, _thread_id=sid,
    ))["artifact"]
    resolved = json.loads(await ArtifactResolveTool().execute(
        path=name, _thread_id=sid,
    ))

    assert registered["path"] == str((integration_workspace / name).resolve())
    assert resolved["artifact"]["id"] == registered["id"]
