"""File and mixed shell calls follow the selection made in the same Turn.

`read_file` and a mixed `bash` read and write run unrecorded without a
selection (`test_plan_tool_dispatch_no_selection.py`).
Here the same Turn first returns a completion with nothing selected, then
selects ordinary work, and each file or shell call reaches its tool and is
recorded under that selection. Plans never classifies a shell command as a
read or an effect; the host gate and the tool keep that decision.
"""

from __future__ import annotations

import pytest

from infra.database import get_db
from plans import admission, service
from tests.event_routing_helpers import mk_session
from tests.plan_execution_helpers import (
    add_task,
    bash_probe,
    delegate_send_probe,
    dispatch_rows,
    execute_as,
    open_turn,
    read_file_probe,
    registry_of,
)


@pytest.mark.asyncio
async def test_file_and_mixed_shell_calls_after_a_return_follow_the_new_selection(
    isolated_db,
):
    """After a completion return, file and shell calls follow the new selection."""
    parent_id = mk_session("file-shell-parent")
    child_id = mk_session("file-shell-child", spawned_by=parent_id)
    db = get_db()
    identity = open_turn(db, child_id)
    plan = service.ensure_plan("thread", child_id, db=db)
    remaining = add_task(db, plan.id, "Remaining obligation")
    db.commit()
    reader = read_file_probe(result="file contents")
    shell = bash_probe(result="shell ran")
    returned = delegate_send_probe(result="completion accepted")
    registry = registry_of(returned, reader, shell)

    completed = await execute_as(
        registry, identity, child_id, returned.name,
        {"thread_id": parent_id, "message": "finished", "intent": "completion"},
    )
    assert completed == "completion accepted"
    assert dispatch_rows(db) == []

    selected = admission.activate(
        remaining.id, expected_revision=remaining.revision, actor_id=child_id,
        provenance="test", db=db,
    )
    db.commit()
    attempt_id = selected["admission"]["selection"]["attempt_id"]
    calls = [
        (reader.name, {"path": "notes.txt"}, "file contents"),
        (shell.name, {"command": "cat notes.txt"}, "shell ran"),
        (shell.name, {"command": "printf done > notes.txt"}, "shell ran"),
    ]
    for name, params, expected in calls:
        assert await execute_as(registry, identity, child_id, name, params) == expected

    assert [
        (row["tool_name"], row["attempt_id"], row["turn_id"], row["outcome"])
        for row in dispatch_rows(db)
    ] == [
        (name, attempt_id, identity.turn_id, "succeeded") for name, _params, _ in calls
    ]
    db.close()
