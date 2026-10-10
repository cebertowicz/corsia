"""Prove every governed file-tool path publishes off the event loop.

A governed write opens SQLite write transactions (``stage_durable_write``,
``apply_durable_write``, ``register_durable_write``, ``complete_durable_write``,
``register_category_index``) and takes an ``fcntl.flock`` on the shared category
index. Both may wait on another writer, so the whole publication runs on the DB
executor; the registry's socket nudge stays on the event loop (see
``test_durable_writes.py::test_corsia_write_and_edit_tools_use_durable_shared_seam``).

``CORSIA_DB_LOOP_WRITE_STRICT=1`` makes any write transaction opened on the loop
raise, so a reintroduced on-loop publication fails these cases and the tool
returns its error text instead of a registry receipt.
"""

from __future__ import annotations

import json

import pytest

from tests.test_durable_writes import durable_scope  # noqa: F401 - shared governed-workspace fixture


def _receipt(text: str) -> dict | None:
    try:
        parsed = json.loads(text)
    except (TypeError, ValueError):
        return None
    return parsed if isinstance(parsed, dict) else None


@pytest.mark.asyncio
async def test_strict_loop_guard_rejects_a_direct_loop_write(durable_scope, monkeypatch):  # noqa: F811
    """The strict switch these cases rely on is armed: an on-loop write raises."""
    from infra.db_loop_guard import LOOP_WRITE_STRICT_ENV, LoopThreadWriteError
    from registry.durable_writes import write_text

    workspace, project_root, thread_id, _db = durable_scope
    target = project_root / "shared" / "planning" / "strict-control.md"
    monkeypatch.setenv(LOOP_WRITE_STRICT_ENV, "1")

    with pytest.raises(LoopThreadWriteError):
        write_text(
            target,
            "control",
            thread_id=thread_id,
            project_id="project-alpha",
            workspace=workspace,
            description="Strict control",
        )
    assert not target.exists()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("name", "initial", "args", "expected"),
    [
        ("write_file", "before write", {"content": "after write"}, "after write"),
        ("edit_file", "before edit", {"old_string": "before", "new_string": "after"}, "after edit"),
        (
            "multi_edit",
            "alpha beta",
            {"edits": [{"old_string": "beta", "new_string": "delta"}]},
            "alpha delta",
        ),
    ],
)
async def test_governed_file_tool_write_leaves_the_event_loop(
    durable_scope,  # noqa: F811
    monkeypatch,
    name,
    initial,
    args,
    expected,
):
    """Each governed tool publishes on the DB executor, never the loop thread."""
    from infra.db_loop_guard import LOOP_WRITE_STRICT_ENV
    from registry.durable_writes import write_text
    from tools import filesystem_edit, filesystem_rw
    from tools.filesystem_edit import EditFileTool, MultiEditTool
    from tools.filesystem_gate import AuthorizedPath
    from tools.filesystem_rw import WriteFileTool

    workspace, project_root, thread_id, db = durable_scope
    target = project_root / "shared" / "planning" / f"{name}-loop-proof.md"
    # Register the shared file before strict mode is armed: the tool call below is
    # the frame under test, not this baseline.
    write_text(
        target,
        initial,
        thread_id=thread_id,
        project_id="project-alpha",
        workspace=workspace,
        description="Loop proof",
    )

    async def authorize(**_kwargs):
        return AuthorizedPath(target, reviewed=False)

    monkeypatch.setattr(filesystem_rw, "_resolve_path_for_action", authorize)
    monkeypatch.setattr(filesystem_edit, "_resolve_path_for_action", authorize)
    monkeypatch.setenv(LOOP_WRITE_STRICT_ENV, "1")

    call_args = {"path": str(target), **args}
    call_kwargs = {"_thread_id": thread_id, "_thread_project_id": "project-alpha"}
    if name == "write_file":
        receipt = await WriteFileTool(workspace=workspace).execute(**call_args, **call_kwargs)
    elif name == "edit_file":
        receipt = await EditFileTool(workspace=workspace).execute(**call_args, **call_kwargs)
    else:
        receipt = await MultiEditTool(workspace=workspace).execute(**call_args, **call_kwargs)

    managed = _receipt(receipt)
    assert managed is not None and managed.get("registry_managed") is True, receipt
    assert target.read_text(encoding="utf-8") == expected
    assert db.execute(
        "SELECT COUNT(*) FROM durable_write_intents WHERE target_path=?", (str(target),)
    ).fetchone()[0] == 0