"""Startup intent recovery precedes lane recovery for the same file."""
import asyncio
import os
from pathlib import Path
import sys
import threading

import pytest

from doceditor.live_document import LiveDocument, file_hash
from doceditor.live_lane import DocumentLane, Operation
from registry import durable_writes
from tests.test_document_live_lane import insert
from tests.test_durable_writes import durable_scope as durable_scope


@pytest.mark.asyncio
@pytest.mark.parametrize("cut", ["register", "undo"])
async def test_killed_writer_recovers_intent_before_lane(durable_scope, cut, tmp_path):
    workspace, project, thread, db = durable_scope
    target = project / "shared" / "planning" / "two-journals.md"
    durable_writes.write_text(
        target, "hello quote", thread_id=thread, project_id="project-alpha", workspace=workspace,
        description="Journal recovery proof", notify=False,
    )
    state = tmp_path / "live.sqlite"
    db_path = Path(db.execute("PRAGMA database_list").fetchone()[2])
    env = {**os.environ, "PYTHONPATH": str(Path(__file__).resolve().parents[1])}
    process = await asyncio.create_subprocess_exec(
        sys.executable, str(Path(__file__).resolve()), str(workspace), str(db_path),
        str(target), str(state), thread, cut, cwd=tmp_path, env=env,
        stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
    )
    try:
        # A crashed child ends the line at EOF; this bound only stops a hang (cold import can exceed 10 s).
        line = await asyncio.wait_for(process.stdout.readline(), 120)
        if line != b"CUT\n":
            _, error = await process.communicate()
            pytest.fail(error.decode())
        process.kill()
        await asyncio.wait_for(process.wait(), 5)
    finally:
        if process.returncode is None:
            process.kill()
            await process.wait()
    assert db.execute("SELECT COUNT(*) FROM durable_write_intents").fetchone()[0] == 1
    # The real application lifecycle runs this before live documents can load.
    durable_writes.recover_write_intents(workspace=workspace)
    assert db.execute("SELECT COUNT(*) FROM durable_write_intents").fetchone()[0] == 0
    live = await LiveDocument.open(target, state)

    async def valid(text, actor=None):
        pass

    async def writer(text, prior_hash):
        await asyncio.to_thread(
            durable_writes.write_text, target, text, thread_id=thread,
            project_id="project-alpha", workspace=workspace, notify=False,
        )

    lane = DocumentLane(live, validate_actor=valid, write=writer)
    assert live.store.pending() is not None
    await lane.recover()
    assert live.store.pending() is None
    expected = "USER hello world" if cut == "register" else "USER hello quote"
    assert live.text == expected
    await lane.submit(Operation("flush"))
    assert target.read_text() == live.text == expected
    await lane.recover()
    assert live.text == expected


async def worker(workspace, db_path, target, state, thread, cut):
    from infra import config, database
    from infra.platform_paths import WorkspaceSelection
    from registry import file_resolution

    config.WORKSPACE = workspace
    config.WORKSPACE_SELECTION = WorkspaceSelection(
        workspace_id="workspace-stable-id", root=workspace, name="Test workspace",
    )
    config.DB_PATH = database.DB_PATH = db_path
    file_resolution.get_allowed_dirs_rw = lambda **kwargs: [workspace]

    def parked(*args, **kwargs):
        print("CUT", flush=True)
        threading.Event().wait()

    if cut == "register":
        durable_writes._register_applied = parked
    else:
        def refused(*args, **kwargs):
            raise PermissionError("registration refused")
        durable_writes._register_applied = refused
        durable_writes._finish_undo = parked

    live = await LiveDocument.open(target, state)
    base = file_hash(target.read_bytes())
    insert(live, "typing")

    async def valid(text, actor=None):
        pass

    async def writer(text, prior_hash):
        await asyncio.to_thread(
            durable_writes.write_text, target, text, thread_id=thread,
            project_id="project-alpha", workspace=workspace, notify=False,
        )

    lane = DocumentLane(live, validate_actor=valid, write=writer)
    await lane.submit(Operation("write", content="hello world", base_hash=base))


if __name__ == "__main__":
    asyncio.run(worker(
        Path(sys.argv[1]), Path(sys.argv[2]), Path(sys.argv[3]), Path(sys.argv[4]),
        sys.argv[5], sys.argv[6],
    ))
