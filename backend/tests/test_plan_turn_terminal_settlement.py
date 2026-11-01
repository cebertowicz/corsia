"""Turn terminal settles open plan dispatches without inventing a hold.

When a Turn ends while a recorded tool call is still open, ``finish_turn`` marks that
dispatch ``unknown`` inside the terminal write. The open call never held a slot:
parking or editing the task already released it (#2705), so the successor Turn
claimed by that terminal carries the selection made then.
No generic ``uncertain_effect`` hold is created; real holds stay binding.
"""

from __future__ import annotations

import pytest

from infra.input_admission import accept_thread_input
from infra.thread_driver import TerminalResult, finish_turn
from plans import admission, service
from plans.boundary import PlanActor, mutate
from tests.test_clean_turn_terminal import _db, _running_turn, _session

pytestmark = pytest.mark.usefixtures("signed_in_defaults")  # a harness turn needs a signed-in Default: no ambient login
THREAD = "thread-1"


@pytest.fixture
def debt_log():
    """Collect the ERROR lines ``settled_or_debt`` writes for refused units."""
    from loguru import logger

    lines: list[str] = []
    sink = logger.add(lambda message: lines.append(str(message)), level="ERROR")
    yield lines
    logger.remove(sink)


def _turn_state(db, turn_id):
    return tuple(db.execute(
        "SELECT status,error FROM active_turns WHERE id=?", (turn_id,),
    ).fetchone())


@pytest.mark.asyncio
async def test_terminal_persists_known_completed_output_after_result_write_failed(isolated_db, monkeypatch):
    from core.turn_core import persist_turn_terminal, terminal_error_detail
    from infra.database import get_db
    from subagents import harness_tool_bridge
    from subagents.child_thread import persist_assistant_step
    from tests.plan_execution_helpers import ProbeTool, registry_of, selected_plan
    from tests.test_cc_tool_result_echo_binding import _seed_thread
    from tools.recorded_call import CompletedToolFailure

    db = get_db()
    identity = _seed_thread(db)
    selected_plan(db, THREAD)
    tool = ProbeTool(result="completed effect retained at terminal")
    step = persist_assistant_step(THREAD, content="", tool_uses=[{
        "id": "call", "name": tool.name, "arguments": {},
    }], turn_id=identity.turn_id, db=db)
    db.commit()

    async def failed(**kwargs):
        raise RuntimeError("result persistence unavailable")

    monkeypatch.setattr(harness_tool_bridge, "_persist_delivery_off_loop", failed)
    with pytest.raises(CompletedToolFailure) as raised:
        await harness_tool_bridge.execute_tool(registry_of(tool), thread_id=THREAD,
            turn_identity=identity, invocation_id="invocation", provider_tool_call_id="call",
            assistant_step_id=step, name=tool.name, arguments={})
    assert raised.value.completed[0].canonical is None
    assert raised.value.completed[0].arguments == {}
    assert db.execute("SELECT COUNT(*) FROM messages WHERE role='tool'").fetchone()[0] == 0
    assert persist_turn_terminal(identity.turn_id, "error", terminal_error_detail(raised.value), db=db) is TerminalResult.APPLIED
    assert persist_turn_terminal(identity.turn_id, "error", terminal_error_detail(raised.value), db=db) is TerminalResult.ALREADY_SAME_OUTCOME
    rows = db.execute("SELECT content,metadata FROM messages WHERE role='tool'").fetchall()
    assert [r["content"] for r in rows] == [tool.result]
    import json
    metadata = json.loads(rows[0]["metadata"])
    assert metadata["tool_invocation_id"] == "invocation" and not metadata.get("synthetic_tool_result")
    assert metadata["assistant_step_id"] == step
    assert tool.calls == 1
    assert db.execute("SELECT status FROM active_turns WHERE id=?", (identity.turn_id,)).fetchone()[0] == "error"
    assert db.execute(
        "SELECT delivery_state FROM messages WHERE id=?",
        (f"{identity.turn_id}:input",),
    ).fetchone()[0] == "delivered"
    db.close()


@pytest.mark.asyncio
@pytest.mark.parametrize("failed_at", ["result_write", "call_settlement"])
async def test_native_terminal_binds_completed_output_to_its_own_step_after_result_write_failed(
    isolated_db, monkeypatch, failed_at,
):
    """A native Turn's completed output keeps the exact Step its handler persisted."""
    import asyncio
    import json
    from types import SimpleNamespace

    from core import turns_steps
    from core.step_handler import StepHandler
    from core.turn_core import persist_turn_terminal, terminal_error_detail
    from infra.database import get_db
    from tests.test_cc_tool_result_echo_binding import _seed_thread
    from tools.recorded_call import CompletedToolCall, CompletedToolFailure

    db = get_db()
    identity = _seed_thread(db)

    async def broadcast(_frame: dict) -> None:
        return None

    handler = StepHandler(THREAD, {"model": "test/model"}, broadcast, identity.turn_id)
    handler.bind_turn_identity(identity)

    async def on_step(kind: str, payload: dict):
        if kind == "tool_complete" and failed_at == "result_write":
            raise RuntimeError("result persistence unavailable")
        return await handler.handle(kind, payload)

    class _Tools:
        async def execute(self, _name: str, _arguments: dict) -> str:
            if failed_at == "call_settlement":
                # recorded_call's shape when settling the finished call fails.
                raise CompletedToolFailure((CompletedToolCall(
                    name="probe", result="native completed output",
                    thread_id=THREAD, turn_id=identity.turn_id, dispatch_id="dispatch",
                ),))
            return "native completed output"

    class _Context:
        @staticmethod
        def add_assistant_message(messages, content, calls, **_kwargs):
            return [*messages, {"role": "assistant", "content": content, "tool_calls": calls}]

    agent = SimpleNamespace(
        tools=_Tools(), context=_Context(), _strip_think=lambda value: value,
        _tool_hint=lambda calls: calls[0].name,
    )
    response = SimpleNamespace(
        content="", reasoning_content=None,
        tool_calls=[SimpleNamespace(id="call", name="probe", arguments={})],
    )
    monkeypatch.setattr(turns_steps, "_current_turn_identity", lambda: identity)
    with pytest.raises(CompletedToolFailure) as raised:
        await turns_steps._execute_tool_calls_inner(
            agent, response, [], THREAD, asyncio.Event(), on_step, [], "test/model",
        )

    assert persist_turn_terminal(
        identity.turn_id, "error", terminal_error_detail(raised.value), db=db,
    ) is TerminalResult.APPLIED
    [row] = db.execute("SELECT content,metadata FROM messages WHERE role='tool'").fetchall()
    assert row["content"] == "native completed output"
    assert json.loads(row["metadata"])["assistant_step_id"] == handler.step_ids[0]
    db.close()


def test_terminal_leaves_completed_output_without_its_exact_step_as_debt(isolated_db, debt_log):
    """Duplicate Step rows cannot be told apart, so only the captured id binds.

    The unbindable result is refused as one named unit of debt; the terminal
    still applies and writes no row for it.
    """
    from core.turn_core import persist_turn_terminal
    from infra.database import get_db
    from subagents.child_thread import persist_assistant_step
    from tests.test_cc_tool_result_echo_binding import _seed_thread
    from tools.recorded_call import CompletedToolCall, CompletedToolFailure

    db = get_db()
    identity = _seed_thread(db)
    step = persist_assistant_step(THREAD, content="", tool_uses=[{
        "id": "call", "name": "probe", "arguments": {"value": 1},
    }], turn_id=identity.turn_id, db=db)
    row = db.execute("SELECT metadata FROM messages WHERE id=?", (step,)).fetchone()
    from infra.queries import insert_message
    import json

    insert_message(
        THREAD, "assistant", "", json.loads(row["metadata"]),
        msg_id="ambiguous-step", db=db,
    )
    db.commit()
    failure = CompletedToolFailure((CompletedToolCall(
        name="probe", arguments={"value": 1}, result="completed output",
        thread_id=THREAD, turn_id=identity.turn_id,
        invocation_id="invocation", provider_tool_call_id="call",
    ),))

    assert persist_turn_terminal(
        identity.turn_id, "error", failure, db=db,
    ) is TerminalResult.APPLIED

    status, error = _turn_state(db, identity.turn_id)
    assert status == "error" and "completed output" not in error
    assert db.execute("SELECT COUNT(*) FROM messages WHERE role='tool'").fetchone()[0] == 0
    [line] = debt_log
    assert "lacks exact Assistant Step identity" in line and "'call'" in line
    assert identity.turn_id in line and THREAD in line and "completed output" not in line
    db.close()


def test_terminal_result_storage_failure_does_not_claim_durability(
    isolated_db, monkeypatch, debt_log,
):
    """A storage failure that leaves the savepoint intact is debt, never a row.

    The terminal applies without claiming the result is durable. A failure that
    takes the transaction with it still raises:
    ``test_retained_unit_debt.test_terminal_lost_savepoint_raises_and_the_turn_stays_running``.
    """
    from core.turn_core import persist_turn_terminal
    from infra.database import get_db
    from subagents.child_thread import persist_assistant_step
    from tests.test_cc_tool_result_echo_binding import _seed_thread
    from tools.recorded_call import CompletedToolCall, CompletedToolFailure

    db = get_db()
    identity = _seed_thread(db)
    step = persist_assistant_step(THREAD, content="", tool_uses=[{
        "id": "call", "name": "probe", "arguments": {},
    }], turn_id=identity.turn_id, db=db)
    db.commit()
    failure = CompletedToolFailure((CompletedToolCall(
        name="probe", arguments={}, result="completed output",
        thread_id=THREAD, turn_id=identity.turn_id,
        invocation_id="invocation", provider_tool_call_id="call",
        assistant_step_id=step,
    ),))

    def unavailable(**kwargs):
        raise OSError("result storage unavailable")

    monkeypatch.setattr("core.tool_result_delivery.persist_tool_execution_result", unavailable)
    assert persist_turn_terminal(
        identity.turn_id, "error", failure, db=db,
    ) is TerminalResult.APPLIED

    status, error = _turn_state(db, identity.turn_id)
    assert status == "error" and "completed output" not in error
    assert db.execute("SELECT COUNT(*) FROM messages WHERE role='tool'").fetchone()[0] == 0
    [line] = debt_log
    assert "OSError" in line and "result storage unavailable" in line
    assert "stays unpaid debt" in line and "completed output" not in line
    db.close()


@pytest.mark.parametrize("conflict", ["invocation", "is_error", "media"])
def test_terminal_keeps_the_durable_result_over_a_nonidentical_retained_one(
    isolated_db, conflict, debt_log,
):
    """A conflicting retained result is refused as debt; the durable row stands."""
    from core.media_reads import IMAGE_KIND, MediaReadReference, MediaToolResult
    from core.tool_result_delivery import persist_tool_execution_result
    from core.turn_core import persist_turn_terminal
    from infra.database import get_db
    from subagents.child_thread import persist_assistant_step
    from tests.test_cc_tool_result_echo_binding import _seed_thread
    from tools.recorded_call import CompletedToolCall, CompletedToolFailure

    db = get_db()
    identity = _seed_thread(db)
    step = persist_assistant_step(THREAD, content="", tool_uses=[{
        "id": "call", "name": "probe", "arguments": {},
    }], turn_id=identity.turn_id, db=db)
    first_media = MediaReadReference("/tmp/first.png", "image/png", IMAGE_KIND, 1, "a" * 64)
    retained_media = MediaReadReference("/tmp/second.png", "image/png", IMAGE_KIND, 1, "b" * 64)
    durable_content = MediaToolResult("same completed output", first_media) if conflict == "media" else "same completed output"
    persist_tool_execution_result(
        thread_id=THREAD, turn_id=identity.turn_id,
        invocation_id="durable-invocation" if conflict == "invocation" else "invocation",
        assistant_step_id=step, provider_tool_call_id="call", name="probe",
        arguments={}, content=durable_content, is_error=conflict == "is_error",
        extra_metadata={"media": first_media.as_metadata()} if conflict == "media" else None,
        db=db,
    )
    db.commit()
    retained_content = MediaToolResult("same completed output", retained_media) if conflict == "media" else "same completed output"
    failure = CompletedToolFailure((CompletedToolCall(
        name="probe", result=retained_content, arguments={}, thread_id=THREAD,
        turn_id=identity.turn_id, invocation_id="invocation",
        provider_tool_call_id="call", assistant_step_id=step,
    ),))

    before = db.execute("SELECT id,content,metadata FROM messages WHERE role='tool'").fetchall()

    assert persist_turn_terminal(
        identity.turn_id, "error", failure, db=db,
    ) is TerminalResult.APPLIED

    assert _turn_state(db, identity.turn_id)[0] == "error"
    after = db.execute("SELECT id,content,metadata FROM messages WHERE role='tool'").fetchall()
    assert [tuple(row) for row in after] == [tuple(row) for row in before] and len(after) == 1
    [line] = debt_log
    assert "conflicts with its durable binding" in line and "'call'" in line
    db.close()


def _task(db, plan_id: str, title: str, order_key: int):
    """Add one DOD-bearing task at an explicit plan position."""
    return service.add_node(
        plan_id, kind="task", title=title,
        acceptance_criteria=f"{title} is verified", order_key=order_key, db=db,
    )


def _selected_task(db, thread_id: str, title: str = "Task A"):
    """Return the thread's plan, one activated task, and its open attempt id."""
    plan = service.ensure_plan("thread", thread_id, db=db)
    node = _task(db, plan.id, title, 0)
    selected = admission.activate(
        node.id, expected_revision=node.revision, actor_id=thread_id,
        provenance="test", db=db,
    )
    return plan, node, selected["admission"]["selection"]["attempt_id"]


def _open_dispatch(db, attempt_id: str, turn_id: str, dispatch_id: str = "dispatch-1"):
    """Record one started call under the attempt that never settled."""
    db.execute(
        """INSERT INTO plan_tool_dispatches (id,attempt_id,turn_id,tool_name,started_at)
           VALUES (?,?,?,'bash','t3')""",
        (dispatch_id, attempt_id, turn_id),
    )


def _mutate(db, plan_id: str, operation: str, params: dict, key: str, actor=None):
    """Apply one plan mutation through the actor-bound boundary.

    Setup rows are committed first: in production the boundary owns the
    outermost write, and its post-commit notice requires a committed state.
    """
    db.commit()
    return mutate(
        plan_id, operation, params, actor=actor or PlanActor(THREAD, "user-1", None),
        provenance="test.turn_terminal", mutation_id=key, db=db,
    ).result


def _settle_task(db, plan_id: str, node_id: str, operation: str) -> None:
    """Move a task to review, and for ``accept`` on to accepted, via the boundary."""
    node = service.get_node(node_id, db=db)
    _mutate(db, plan_id, "request_review", {
        "node_id": node_id, "evidence": "Focused proof passed",
        "expected_revision": node.revision,
    }, f"review-{node_id}")
    if operation == "accept":
        node = service.get_node(node_id, db=db)
        _mutate(db, plan_id, "accept", {
            "node_id": node_id, "verification_result": "passed",
            "expected_revision": node.revision,
        }, f"accept-{node_id}")


def _queue_next_input(db, thread_id: str) -> None:
    """Queue one input so the terminal claims a successor Turn."""
    accept_thread_input(
        thread_id, role="user", content="next", intent="queue_after_turn",
        message_id=f"{thread_id}-next", db=db,
    )


def _attempt(db, attempt_id: str):
    """Return one attempt row by id."""
    return db.execute(
        "SELECT * FROM plan_node_attempts WHERE id=?", (attempt_id,),
    ).fetchone()


def _open_attempt(db, node_id: str):
    """Return the task's open attempt row, if any."""
    return db.execute(
        "SELECT * FROM plan_node_attempts WHERE node_id=? AND released_at IS NULL",
        (node_id,),
    ).fetchone()


def _dispatch(db, dispatch_id: str = "dispatch-1"):
    """Return one dispatch's settlement outcome and time."""
    return db.execute(
        "SELECT outcome,settled_at FROM plan_tool_dispatches WHERE id=?",
        (dispatch_id,),
    ).fetchone()


def _holds(db, node_id: str) -> list[tuple]:
    """Return every hold ever placed on the task, cleared or not."""
    return [tuple(row) for row in db.execute(
        "SELECT kind,reason_key,cleared_at FROM plan_node_holds WHERE node_id=?",
        (node_id,),
    )]


def _turn_attempt(db, turn_id: str):
    """Return the attempt a Turn's accounting snapshot is bound to."""
    return db.execute(
        "SELECT plan_attempt_id FROM active_turns WHERE id=?", (turn_id,),
    ).fetchone()[0]


def _stop(db, turn_id: str):
    """Commit the explicit Stop terminal the runtime uses for this Turn."""
    return finish_turn(
        turn_id, outcome="cancelled", ended_at="t4",
        successor_turn_id=f"{turn_id}-next", boot_epoch="boot-1", db=db,
    )


def _stopped_selection(db, thread_id: str, *, call_open: bool) -> dict:
    """Stop a Turn working a selected task, with or without an open call."""
    _session(db, thread_id)
    _plan, node, attempt_id = _selected_task(db, thread_id)
    turn_id = f"{thread_id}-turn"
    db.commit()
    _running_turn(db, thread_id, turn_id)
    if call_open:
        _open_dispatch(db, attempt_id, turn_id, dispatch_id=f"{thread_id}-call")
    assert _stop(db, turn_id).result is TerminalResult.APPLIED
    attempt = _attempt(db, attempt_id)
    projected = admission.project(node.id, db=db)
    selection = projected["selection"] or {}
    return {
        "released_at": attempt["released_at"],
        "release_reason": attempt["release_reason"],
        "condition": projected["condition"],
        "selected": selection.get("attempt_id") == attempt_id,
        "in_flight": selection.get("in_flight_dispatch_count"),
        "holds": _holds(db, node.id),
    }


def test_stop_mid_call_keeps_the_selection_a_stop_between_calls_keeps() -> None:
    """Stop with a call still open leaves selected work exactly as Stop between calls.

    The open call settles as ``unknown``; the still-valid attempt stays open and
    running with no hold, identical to a Stop that found no call in flight.
    """
    db = _db()
    try:
        db.commit()
        between = _stopped_selection(db, "between-calls", call_open=False)
        mid_call = _stopped_selection(db, "mid-call", call_open=True)

        assert between == {
            "released_at": None, "release_reason": None, "condition": "running",
            "selected": True, "in_flight": 0, "holds": [],
        }
        assert mid_call == between
        assert _dispatch(db, "mid-call-call")["outcome"] == "unknown"
    finally:
        db.close()


def test_stop_mid_call_under_a_real_user_pause_keeps_the_pause_and_its_release() -> None:
    """A real user pause releases the work at once, open call or not (#2705).

    Stop's terminal then only settles the call; it does not reselect the task,
    and the user's pause is the only hold and remains uncleared.
    """
    db = _db()
    try:
        _session(db, THREAD)
        plan, task, attempt_id = _selected_task(db, THREAD)
        db.commit()
        _running_turn(db, THREAD, "turn-1")
        _open_dispatch(db, attempt_id, "turn-1")
        node = service.get_node(task.id, db=db)
        _mutate(db, plan.id, "pause_node", {
            "node_id": task.id, "reason": "Hold for the user",
            "expected_revision": node.revision,
        }, "user-pause", actor=PlanActor(None, "user-1", None, kind="user"))
        assert _attempt(db, attempt_id)["release_reason"] == "paused"

        assert _stop(db, "turn-1").result is TerminalResult.APPLIED

        assert _dispatch(db)["outcome"] == "unknown"
        assert _attempt(db, attempt_id)["release_reason"] == "paused"
        assert _open_attempt(db, task.id) is None
        assert _holds(db, task.id) == [("paused", "manual_pause", None)]
        assert admission.project(task.id, db=db)["condition"] == "paused"
    finally:
        db.close()


@pytest.mark.usefixtures("no_taken_in_rerun")
def test_terminal_settles_only_the_ending_turns_open_calls() -> None:
    """Another live Turn's in-flight call is not this terminal's to settle.

    Settlement is scoped to the ending Turn's own dispatches, so a call still
    running under another executor's Turn stays open under its attempt.
    """
    db = _db()
    try:
        _session(db, THREAD)
        _session(db, "thread-2")
        _plan, _task_1, attempt_1 = _selected_task(db, THREAD)
        _plan_2, task_2, attempt_2 = _selected_task(db, "thread-2")
        db.commit()
        _running_turn(db, THREAD, "turn-1")
        _running_turn(db, "thread-2", "turn-2")
        _open_dispatch(db, attempt_1, "turn-1")
        _open_dispatch(db, attempt_2, "turn-2", dispatch_id="dispatch-2")

        result = finish_turn(
            "turn-1", outcome="error", ended_at="t4", successor_turn_id="turn-1-next",
            boot_epoch="boot-1", error="backend restarted", db=db,
        )

        assert result.result is TerminalResult.APPLIED
        assert _dispatch(db)["outcome"] == "unknown"
        assert tuple(_dispatch(db, "dispatch-2")) == (None, None)
        selection = admission.project(task_2.id, db=db)["selection"]
        assert selection["attempt_id"] == attempt_2
        assert selection["in_flight_dispatch_count"] == 1
    finally:
        db.close()


@pytest.mark.parametrize("operation", ["request_review", "accept"])
def test_settling_a_mid_call_hands_its_slot_on_and_terminal_carries_b(operation) -> None:
    """Review or acceptance of A hands its slot to queued B at once (#2705).

    A's open call does not hold the slot: A is released as ``review_requested``
    and B selected with no hold and no ``activate``/``start``. The Turn terminal
    settles A's call and the successor Turn it claims carries B.
    """
    db = _db()
    try:
        _session(db, THREAD)
        plan, task_a, attempt_a = _selected_task(db, THREAD)
        task_b = _task(db, plan.id, "Task B", 1)
        # B is active but queued: A's attempt holds this executor's slot.
        service.transition_node(task_b.id, "active", db=db)
        db.commit()
        _running_turn(db, THREAD, "turn-1")
        _open_dispatch(db, attempt_a, "turn-1")
        _settle_task(db, plan.id, task_a.id, operation)
        _queue_next_input(db, THREAD)
        assert _attempt(db, attempt_a)["release_reason"] == "review_requested"
        successor = _open_attempt(db, task_b.id)
        assert successor is not None

        result = finish_turn(
            "turn-1", outcome="completed", ended_at="t4",
            successor_turn_id="turn-2", boot_epoch="boot-1", db=db,
        )

        assert result.result is TerminalResult.APPLIED
        assert _dispatch(db)["outcome"] == "unknown"
        assert _open_attempt(db, task_b.id)["id"] == successor["id"]
        assert admission.project(task_b.id, db=db)["condition"] == "running"
        assert db.execute("SELECT COUNT(*) FROM plan_node_holds").fetchone()[0] == 0
        # Same transaction: the successor Turn this terminal claimed carries B.
        assert _turn_attempt(db, "turn-2") == successor["id"]
    finally:
        db.close()


def test_a_requirements_edit_mid_call_reselects_the_task_at_once() -> None:
    """A requirements edit during an open call reselects the task at once (#2705).

    The edit releases the stale attempt and selects the same task again at
    requirements revision 2, instead of blocking it; the terminal only settles
    the call, and the successor Turn carries the new attempt.
    """
    db = _db()
    try:
        _session(db, THREAD)
        plan, task, stale_attempt = _selected_task(db, THREAD)
        db.commit()
        _running_turn(db, THREAD, "turn-1")
        _open_dispatch(db, stale_attempt, "turn-1")
        node = service.get_node(task.id, db=db)
        _mutate(db, plan.id, "edit", {
            "node_id": task.id, "acceptance_criteria": "The revised requirement is verified",
            "expected_revision": node.revision,
        }, "edit-requirements")
        _queue_next_input(db, THREAD)
        assert _attempt(db, stale_attempt)["release_reason"] == "requirements_changed"

        result = finish_turn(
            "turn-1", outcome="error", ended_at="t4", successor_turn_id="turn-2",
            boot_epoch="boot-1", error="backend restarted", db=db,
        )

        reselected = _open_attempt(db, task.id)
        assert result.result is TerminalResult.APPLIED
        assert _dispatch(db)["outcome"] == "unknown"
        assert reselected is not None
        assert (reselected["requirements_revision"], reselected["attempt_ordinal"]) == (2, 2)
        assert _holds(db, task.id) == []
        projected = admission.project(task.id, db=db)
        assert projected["condition"] == "running"
        assert projected["selection"]["attempt_id"] == reselected["id"]
        assert _turn_attempt(db, "turn-2") == reselected["id"]
    finally:
        db.close()


def _node_op(db, plan_id: str, operation: str, node_id: str, key: str, **params) -> dict:
    """Run one node operation through the boundary at the node's current revision."""
    node = service.get_node(node_id, db=db)
    return _mutate(
        db, plan_id, operation,
        {"node_id": node_id, "expected_revision": node.revision, **params}, key,
    )


def test_an_edited_task_keeps_its_slot_ahead_of_earlier_queued_work() -> None:
    """The edited task takes its slot back at once while earlier work stays queued.

    Task A's submission is rejected while Task B holds the executor's slot; the
    executor takes up A's revision, which is queued. B's requirements change
    while its call is open; the edit releases B's stale attempt and selects B
    again at requirements revision 2, the terminal leaves that alone, and the
    revision still names B as the task in its way.
    """
    db = _db()
    try:
        _session(db, THREAD)
        plan = service.ensure_plan("thread", THREAD, db=db)
        task_a = _task(db, plan.id, "Task A", 0)
        task_b = _task(db, plan.id, "Task B", 1)
        _node_op(db, plan.id, "activate", task_a.id, "activate-a")
        _node_op(
            db, plan.id, "request_review", task_a.id, "review-a",
            evidence="Focused proof passed",
        )
        _node_op(db, plan.id, "activate", task_b.id, "activate-b")
        rejected = _node_op(
            db, plan.id, "reject", task_a.id, "reject-a",
            feedback="The proof misses the empty-input case",
        )
        revision_a = rejected["revision"]["node_id"]
        _node_op(db, plan.id, "activate", revision_a, "activate-a2")
        stale_attempt = _open_attempt(db, task_b.id)["id"]
        _running_turn(db, THREAD, "turn-1")
        _open_dispatch(db, stale_attempt, "turn-1")
        _node_op(
            db, plan.id, "edit", task_b.id, "edit-b",
            acceptance_criteria="The revised requirement is verified",
        )
        _queue_next_input(db, THREAD)
        assert _attempt(db, stale_attempt)["release_reason"] == "requirements_changed"

        result = finish_turn(
            "turn-1", outcome="error", ended_at="t4", successor_turn_id="turn-2",
            boot_epoch="boot-1", error="backend restarted", db=db,
        )

        reselected = _open_attempt(db, task_b.id)
        assert result.result is TerminalResult.APPLIED
        assert reselected is not None, "the edited task lost its slot"
        assert reselected["requirements_revision"] == 2
        assert _open_attempt(db, revision_a) is None
        queued = admission.project(revision_a, db=db)
        assert queued["condition"] == "ready"
        assert queued["conflict"]["selected_node_id"] == task_b.id
    finally:
        db.close()
