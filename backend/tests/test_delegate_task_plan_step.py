"""delegate_task hands the new worker a plan task (decision b27dda38).

By default the worker takes over the caller's current task; ``plan_node_id``
names another or ``'none'`` keeps it. A named task that cannot be handed over
is refused before anything is spawned, and the result's ``plan_task`` always
says what happened.
"""

from __future__ import annotations

import json

import pytest

from delegation_fixtures import deleg_db  # noqa: F401
from plans import admission, admission_attempts, public, service
from plans.delegate_attach import ATTACHED, KEPT, NO_TASK

pytestmark = pytest.mark.asyncio

_PARENT = "parent-1"


def _seed_parent(db) -> str:
    db.execute(
        "INSERT INTO threads (id,user_id,project_id,model,title,created_at,updated_at) "
        "VALUES (?,'u-test','proj-1','claude-code/opus','Parent','t','t')",
        (_PARENT,),
    )
    db.commit()
    return service.ensure_plan("thread", _PARENT, db=db).id


def _task(db, plan_id: str, title: str, *, current: bool = False, intent: str | None = None) -> str:
    node = service.add_node(
        plan_id, kind="task", title=title, acceptance_criteria=f"{title} done", intent=intent, db=db,
    )
    service.transition_node(node.id, "active", db=db)
    if current:
        revision = service.get_node(node.id, db=db).revision
        admission.start(
            node.id, expected_revision=revision, actor_id=_PARENT,
            provenance="test", db=db,
        )
    db.commit()
    return node.id


@pytest.fixture
def spawned(deleg_db, monkeypatch):
    """Patch the native spawn so each worker is a real subagent row of the parent."""
    children: list[str] = []

    async def _create(**kwargs):
        child = f"child-{len(children) + 1}"
        deleg_db.execute(
            "INSERT INTO threads (id,user_id,project_id,spawned_by,model,"
            "title,created_at,updated_at) VALUES (?,?,?,?,?,?,'t','t')",
            (child, kwargs["user_id"], kwargs["project_id"], kwargs["spawned_by"],
             kwargs["model"], kwargs["title"]),
        )
        deleg_db.commit()
        children.append(child)
        return {"id": child, "user_id": kwargs["user_id"]}

    async def _send(thread_id, _row, task, **_kwargs):
        # The real send queues the task before the hand-over runs; use the
        # canonical input writer so the fixture advances the FIFO cursor too.
        from infra.input_admission import accept_thread_input

        accept_thread_input(
            thread_id, role="user", content=task, intent="apply_now",
            message_id=f"delegate-task-input:{thread_id}", db=deleg_db,
        )
        return type("Sent", (), {"driver": "turn_engine", "duplicate": False})()

    monkeypatch.setattr("tools.delegate_task.create_visible_fresh_thread", _create)
    monkeypatch.setattr("tools.delegate_task.send_thread_input", _send)
    monkeypatch.setattr("tools.delegate_task.eligible_delegation_model", lambda m, **_scope: (m, None))
    return children


async def _delegate(**kwargs) -> str:
    from tools.delegate_task import DelegateTaskTool

    return await DelegateTaskTool().execute(
        task="do the work", model="omlx@local/qwen", _thread_id=_PARENT, **kwargs,
    )


async def _receipt(**kwargs) -> dict:
    text = await _delegate(**kwargs)
    assert text.startswith("{"), text
    return json.loads(text)


def _worker(db, node_id: str):
    return db.execute(
        "SELECT spawned_thread_id FROM plan_nodes WHERE id=?", (node_id,),
    ).fetchone()["spawned_thread_id"]


def _holds_slot(db) -> list[str]:
    rows = admission_attempts.current_for_executor(db, _PARENT)
    return [str(row["node_id"]) for row in rows]


async def test_worker_takes_over_the_current_task_and_says_so(deleg_db, spawned):
    plan_id = _seed_parent(deleg_db)
    node_id = _task(deleg_db, plan_id, "Build the thing", current=True)

    result = (await _receipt())

    assert result["thread_id"] == "child-1"
    assert result["plan_task"] == {
        "attached": True, "node_id": node_id, "title": "Build the thing",
        "note": ATTACHED.format(title="Build the thing", node_id=node_id),
    }
    assert _worker(deleg_db, node_id) == "child-1"
    assert _holds_slot(deleg_db) == []  # the task left the caller's slot


@pytest.mark.parametrize(
    ("changes", "expected_intent"),
    (
        ({"intent": "Revised intent"}, "Revised intent"),
        ({"acceptance_criteria": "Revised criteria"}, "Initial intent"),
        ({"intent_words": [{
            "quote": "The new constraint", "author": "agent", "role": "bears_on",
            "source_kind": "message", "source_id": _PARENT,
        }]}, "Initial intent"),
    ),
)
async def test_delegate_task_worker_gets_each_intent_revision_once(deleg_db, spawned, changes, expected_intent):
    from plans import mutations

    plan_id = _seed_parent(deleg_db)
    node_id = _task(deleg_db, plan_id, "Build the thing", current=True, intent="Initial intent")
    worker = (await _receipt())["thread_id"]

    updated = mutations.edit_node(
        node_id, expected_revision=service.get_node(node_id, db=deleg_db).revision,
        actor_id=_PARENT, db=deleg_db, **changes,
    )

    rows = deleg_db.execute(
        "SELECT id,content,metadata FROM messages WHERE thread_id=? AND id LIKE 'plan-wake:intent_revised:%'",
        (worker,),
    ).fetchall()
    assert len(rows) == 1
    assert rows[0]["id"] == f"plan-wake:intent_revised:{node_id}:{updated.requirements_revision}:{worker}"
    line, payload = rows[0]["content"].split("\n", 1)
    assert f"revision {updated.requirements_revision}" in line
    assert json.loads(rows[0]["metadata"])["event_type"] == "plan.intent_revised"
    facts = json.loads(payload)["intent_revision"]
    assert facts["intent"] == expected_intent
    assert facts["acceptance_criteria"] == updated.acceptance_criteria
    if "intent_words" in changes:
        assert [word["quote"] for word in facts["intent_words"]] == ["The new constraint"]


async def test_delegate_task_worker_is_not_woken_by_nonrequirements_edits_or_its_own_edit(deleg_db, spawned):
    from plans import mutations

    plan_id = _seed_parent(deleg_db)
    node_id = _task(deleg_db, plan_id, "Build the thing", current=True, intent="Initial intent")
    worker = (await _receipt())["thread_id"]
    mutations.edit_node(node_id, description="Working note", actor_id=_PARENT, db=deleg_db)
    mutations.edit_node(node_id, size="S", actor_id=_PARENT, db=deleg_db)
    mutations.edit_node(node_id, intent="Worker correction", actor_id=worker, db=deleg_db)

    assert deleg_db.execute(
        "SELECT 1 FROM messages WHERE thread_id=? AND id LIKE 'plan-wake:intent_revised:%'", (worker,),
    ).fetchone() is None


async def test_delegate_task_worker_with_an_inheriting_step_gets_one_wake(deleg_db, spawned):
    from plans import mutations

    plan_id = _seed_parent(deleg_db)
    node_id = _task(deleg_db, plan_id, "Build the thing", current=True, intent="Initial intent")
    worker = (await _receipt())["thread_id"]
    child_plan = service.ensure_plan("thread", worker, db=deleg_db)
    child = service.add_node(
        child_plan.id, kind="task", title="Implement", acceptance_criteria="Proven", intent_node_id=node_id, db=deleg_db,
    )
    service.transition_node(child.id, "active", actor_id=worker, db=deleg_db)

    mutations.edit_node(node_id, intent="Updated parent intent", actor_id=_PARENT, db=deleg_db)

    assert deleg_db.execute(
        "SELECT COUNT(*) FROM messages WHERE thread_id=? AND id LIKE 'plan-wake:intent_revised:%'", (worker,),
    ).fetchone()[0] == 1


async def test_no_current_task_starts_unattached_and_says_so(deleg_db, spawned):
    plan_id = _seed_parent(deleg_db)
    node_id = _task(deleg_db, plan_id, "Not started")

    result = (await _receipt())

    assert result["plan_task"] == {"attached": False, "note": NO_TASK}
    assert _worker(deleg_db, node_id) is None


async def test_caller_without_a_plan_starts_unattached(deleg_db, spawned):
    deleg_db.execute(
        "INSERT INTO threads (id,user_id,model,title,created_at,updated_at) "
        "VALUES (?,'u-test','claude-code/opus','Parent','t','t')", (_PARENT,),
    )
    deleg_db.commit()

    result = (await _receipt())

    assert result["plan_task"] == {"attached": False, "note": NO_TASK}


async def test_none_keeps_the_current_task(deleg_db, spawned):
    plan_id = _seed_parent(deleg_db)
    node_id = _task(deleg_db, plan_id, "Mine", current=True)

    result = (await _receipt(plan_node_id="none"))

    assert result["plan_task"] == {"attached": False, "note": KEPT}
    assert _worker(deleg_db, node_id) is None
    assert _holds_slot(deleg_db) == [node_id]


async def test_named_task_is_handed_over_instead_of_the_current_one(deleg_db, spawned):
    plan_id = _seed_parent(deleg_db)
    current = _task(deleg_db, plan_id, "Mine", current=True)
    other = _task(deleg_db, plan_id, "Theirs")

    result = (await _receipt(plan_node_id=other))

    assert result["plan_task"]["attached"] is True
    assert result["plan_task"]["node_id"] == other
    assert _worker(deleg_db, other) == "child-1"
    assert _worker(deleg_db, current) is None
    assert _holds_slot(deleg_db) == [current]


@pytest.mark.parametrize("case", ["unknown", "finished", "has_worker"])
async def test_a_task_that_cannot_be_handed_over_is_refused_before_spawn(
    deleg_db, spawned, case,
):
    plan_id = _seed_parent(deleg_db)
    node_id = _task(deleg_db, plan_id, "Target")
    if case == "finished":
        service.transition_node(node_id, "failed", db=deleg_db)
    if case == "has_worker":
        await _delegate(plan_node_id=node_id)
        spawned.clear()
    deleg_db.commit()

    result = await _delegate(plan_node_id="nope" if case == "unknown" else node_id)

    assert result.startswith("Error: invalid request, fix and resend - ")
    assert spawned == []  # nothing was started


@pytest.mark.parametrize("error", [
    public.PlanError("node already delegates to child-9"),
    RuntimeError("node already delegates to child-9"),  # e.g. a locked database
])
async def test_a_refused_hand_over_after_spawn_says_how_to_attach(
    deleg_db, spawned, monkeypatch, error,
):
    """Any hand-over failure keeps the running worker's receipt and names it."""
    plan_id = _seed_parent(deleg_db)
    node_id = _task(deleg_db, plan_id, "Raced", current=True)

    def _refuse(*_args, **_kwargs):
        raise error

    monkeypatch.setattr(public, "hand_over_delegated_step", _refuse)

    result = (await _receipt())

    task = result["plan_task"]
    assert task["attached"] is False and task["node_id"] == node_id
    assert "node already delegates to child-9" in task["note"]
    assert "attach_spawned_thread, spawned_thread_id child-1" in task["note"]


async def test_a_worker_that_delegates_is_told_its_brief_is_still_its_own(deleg_db, spawned):
    """Guidance only (decision 6c8347eb): the hand-off still happens, at any depth."""
    from tools.delegate_task import WORKER_NOTE

    plan_id = _seed_parent(deleg_db)
    deleg_db.execute("UPDATE threads SET spawned_by='sender-self' WHERE id=?", (_PARENT,))
    node_id = _task(deleg_db, plan_id, "Part of my brief", current=True)

    result = (await _receipt())

    assert result["worker_note"] == WORKER_NOTE
    assert result["plan_task"]["attached"] is True  # nothing is refused
    assert _worker(deleg_db, node_id) == "child-1"


async def test_an_agent_or_chat_that_delegates_never_sees_the_worker_note(deleg_db, spawned):
    plan_id = _seed_parent(deleg_db)
    _task(deleg_db, plan_id, "Mine", current=True)

    result = (await _receipt())

    assert "worker_note" not in result


async def test_a_persistent_agent_with_a_spawner_never_sees_the_worker_note(deleg_db, spawned):
    """An agent is not a worker even when another thread started it."""
    plan_id = _seed_parent(deleg_db)
    deleg_db.execute(
        "UPDATE threads SET spawned_by='sender-self', agent_enabled=1 WHERE id=?", (_PARENT,),
    )
    _task(deleg_db, plan_id, "Mine", current=True)

    result = (await _receipt())

    assert "worker_note" not in result


def _first_input(db, worker: str) -> str:
    return db.execute(
        "SELECT content FROM messages WHERE thread_id=? ORDER BY seq LIMIT 1", (worker,),
    ).fetchone()["content"]


async def test_the_workers_first_input_names_its_task_and_the_read_that_works(deleg_db, spawned):
    from tools.plans import PlanReadTool

    plan_id = _seed_parent(deleg_db)
    node_id = _task(deleg_db, plan_id, "Build the thing", current=True)

    worker = (await _receipt())["thread_id"]

    assert _first_input(deleg_db, worker) == (
        "do the work\n\n"
        f'Your plan task: "Build the thing" ({node_id}). Read its intent and acceptance '
        f"criteria with plan_read(thread_id='{_PARENT}', node_id='{node_id}') before you "
        "start, and again if you're told it changed."
    )
    assert "Build the thing done" not in _first_input(deleg_db, worker)
    read = json.loads(await PlanReadTool().execute(
        node_id=node_id, thread_id=_PARENT, _thread_id=worker,
    ))
    assert read["acceptance_criteria"] == "Build the thing done"


async def test_the_workers_first_input_is_unchanged_without_a_task(deleg_db, spawned):
    plan_id = _seed_parent(deleg_db)
    _task(deleg_db, plan_id, "Build the thing", current=True)

    worker = (await _receipt(plan_node_id="none"))["thread_id"]

    assert _first_input(deleg_db, worker) == "do the work"
