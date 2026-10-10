"""A dispatch row exists exactly for a call that actually started.

Calls refused by a native boundary before the tool starts - parameter
validation, restart draining, host access, the reserved-context check,
repository policy - record nothing and still return that boundary's own text.
A call that started keeps durable ownership: it settles exactly
once with its real outcome, including when the caller is cancelled during the
tool or right after the row was committed.
"""

from __future__ import annotations

import asyncio
from contextlib import contextmanager

import pytest

from automation.restart import RESTART_DRAIN_REFUSAL
import infra.database as database
from infra.database import get_db
from tests.event_routing_helpers import mk_session
from tests.plan_execution_helpers import (
    ERROR_HINT,
    ProbeTool,
    bash_probe,
    dispatch_rows,
    execute_as,
    open_attempt_ids,
    open_turn,
    registry_of,
    selected_plan,
)
from tools.host_access_gate import HostAccessBlocked, HostActionAuthorization

_DRAINING = RESTART_DRAIN_REFUSAL
_HOST_DENIAL = "Host action was blocked by independent safety review."


class _RaisingProbe(ProbeTool):
    """A probe whose execution starts and then raises."""

    async def execute(self, **_kwargs):
        self.calls += 1
        raise RuntimeError("probe exploded")


class _BlockingProbe(ProbeTool):
    """A probe that starts and then waits until its caller is cancelled."""

    def __init__(self) -> None:
        super().__init__("blocking_probe")
        self.started = asyncio.Event()

    async def execute(self, **_kwargs):
        self.calls += 1
        self.started.set()
        await asyncio.Event().wait()
        return "unreachable"


async def _refuse_host_action(**_kwargs):
    raise HostAccessBlocked(_HOST_DENIAL)


async def _forge_reserved_context(**_kwargs):
    """A host verdict that tries to override backend-owned thread identity."""
    return HostActionAuthorization(_parameter_overrides=(("_thread_id", "forged"),))


def _recorded(db) -> list[tuple[str, str | None]]:
    """Return (tool name, outcome) for every dispatch row."""
    return [(row["tool_name"], row["outcome"]) for row in dispatch_rows(db)]


def _selected_executor(db, name: str):
    thread_id = mk_session(name)
    identity = open_turn(db, thread_id)
    _plan, selected = selected_plan(db, thread_id)
    return thread_id, identity, selected["admission"]["selection"]["attempt_id"]


@pytest.mark.asyncio
async def test_invalid_parameters_record_nothing(isolated_db):
    thread_id, identity, _attempt = _selected_executor(get_db(), "own-invalid-params")
    tool = bash_probe()

    result = await execute_as(registry_of(tool), identity, thread_id, "bash", {})

    assert result.startswith("Error: Invalid parameters for tool 'bash': ")
    assert result.endswith(ERROR_HINT)
    assert tool.calls == 0
    assert _recorded(get_db()) == []


@pytest.mark.asyncio
async def test_restart_drain_refusal_records_nothing(isolated_db, monkeypatch):
    """The drain check runs before recording; its refusal is not a started call."""
    monkeypatch.setattr(
        "automation.self_restart.restart_holds_new_work", lambda **_kwargs: True,
    )
    thread_id, identity, _attempt = _selected_executor(get_db(), "own-restart-drain")
    tool = ProbeTool()

    result = await execute_as(registry_of(tool), identity, thread_id, tool.name)

    assert result == _DRAINING
    assert tool.calls == 0
    assert _recorded(get_db()) == []


@pytest.mark.asyncio
async def test_host_access_denial_records_nothing(isolated_db, monkeypatch):
    """Host access decides before recording; its denial text is unchanged."""
    monkeypatch.setattr(
        "tools.host_access_gate.authorize_host_action", _refuse_host_action,
    )
    thread_id, identity, _attempt = _selected_executor(get_db(), "own-host-denial")
    tool = ProbeTool()

    result = await execute_as(registry_of(tool), identity, thread_id, tool.name)

    assert result == f"Error executing uncataloged_probe: {_HOST_DENIAL}{ERROR_HINT}"
    assert tool.calls == 0
    assert _recorded(get_db()) == []


@pytest.mark.asyncio
async def test_repository_policy_refusal_records_nothing(isolated_db):
    """Repository policy decides before recording.

    A real repository policy transition is in progress, so the policy's own
    effect admission refuses the call. Its text reaches the caller unchanged
    and no dispatch row exists, although the executor has a valid selection.
    """
    from infra.repo_policy_transition import repo_policy_transition

    thread_id, identity, attempt_id = _selected_executor(get_db(), "own-repo-policy")
    tool = ProbeTool()

    with repo_policy_transition(active_process_count=lambda: 0):
        result = await execute_as(registry_of(tool), identity, thread_id, tool.name)

    assert result == (
        "Error executing uncataloged_probe: Repository tool effect is blocked by "
        "an active policy transition." + ERROR_HINT
    )
    assert tool.calls == 0
    assert _recorded(get_db()) == []
    assert open_attempt_ids(get_db()) == {attempt_id}


@pytest.mark.asyncio
async def test_reserved_host_override_refusal_records_nothing(isolated_db, monkeypatch):
    """A host verdict that tries to set reserved context is refused.

    The registry alone supplies backend identity; the refusal happens before
    recording, so the native error returns and no dispatch row exists.
    """
    monkeypatch.setattr(
        "tools.host_access_gate.authorize_host_action", _forge_reserved_context,
    )
    thread_id, identity, _attempt = _selected_executor(get_db(), "own-reserved")
    tool = ProbeTool()

    result = await execute_as(registry_of(tool), identity, thread_id, tool.name)

    assert result == (
        "Error executing uncataloged_probe: host-action gate returned reserved "
        "execution context" + ERROR_HINT
    )
    assert tool.calls == 0
    assert _recorded(get_db()) == []


@pytest.mark.asyncio
async def test_a_tool_reported_error_settles_refused(isolated_db):
    thread_id, identity, attempt_id = _selected_executor(get_db(), "own-refused")
    tool = ProbeTool(result="Error: target refused the effect")

    result = await execute_as(registry_of(tool), identity, thread_id, tool.name)

    assert result == "Error: target refused the effect" + ERROR_HINT
    [dispatch] = dispatch_rows(get_db())
    assert dispatch["attempt_id"] == attempt_id
    assert dispatch["outcome"] == "refused"
    assert dispatch["settled_at"] is not None


@pytest.mark.asyncio
async def test_a_raised_exception_settles_failed(isolated_db):
    thread_id, identity, attempt_id = _selected_executor(get_db(), "own-failed")
    tool = _RaisingProbe()

    result = await execute_as(registry_of(tool), identity, thread_id, tool.name)

    assert result == "Error executing uncataloged_probe: probe exploded" + ERROR_HINT
    [dispatch] = dispatch_rows(get_db())
    assert dispatch["attempt_id"] == attempt_id
    assert dispatch["outcome"] == "failed"
    assert dispatch["settled_at"] is not None


@pytest.mark.asyncio
async def test_cancellation_during_the_tool_settles_unknown(isolated_db):
    """A cancelled caller still closes the started call as `unknown`.

    This is registry-level cancellation of the calling task, the signal a
    harness Stop delivers to its admitted tool tasks; the runtime Stop boundary
    itself is exercised in `test_plan_tool_dispatch_runtime_stop.py`.
    """
    thread_id, identity, attempt_id = _selected_executor(get_db(), "own-cancel-tool")
    tool = _BlockingProbe()
    call = asyncio.create_task(
        execute_as(registry_of(tool), identity, thread_id, tool.name),
    )
    await asyncio.wait_for(tool.started.wait(), timeout=5)

    call.cancel()
    with pytest.raises(asyncio.CancelledError):
        await call

    [dispatch] = dispatch_rows(get_db())
    assert dispatch["attempt_id"] == attempt_id
    assert dispatch["settled_at"] is not None
    assert dispatch["outcome"] == "unknown"


class _CancelAfterDispatchCommit:
    """DB-job boundary that cancels the caller once a dispatch row is committed.

    It wraps the real executor-job boundary, so the database work finishes
    exactly as in production; only the awaiting task's cancellation is timed
    to land right after the job that first leaves an unsettled dispatch row.
    """

    def __init__(self, db_path, loop: asyncio.AbstractEventLoop) -> None:
        self._real = database.db_job_boundary
        self._db_path = db_path
        self._loop = loop
        self.task: asyncio.Task | None = None
        self.fired = False

    @contextmanager
    def __call__(self, scope: str):
        with self._real(scope):
            yield
        if self.fired or self.task is None or not self._open_dispatch():
            return
        self.fired = True
        self._loop.call_soon_threadsafe(self.task.cancel)

    def _open_dispatch(self) -> bool:
        import sqlite3

        connection = sqlite3.connect(str(self._db_path))
        try:
            return connection.execute(
                "SELECT 1 FROM plan_tool_dispatches WHERE settled_at IS NULL",
            ).fetchone() is not None
        finally:
            connection.close()


@pytest.mark.asyncio
async def test_cancellation_right_after_recording_still_settles_unknown(
    isolated_db, monkeypatch,
):
    """The dispatch id exists before recording, so no started row is orphaned."""
    thread_id, identity, attempt_id = _selected_executor(get_db(), "own-cancel-record")
    boundary = _CancelAfterDispatchCommit(isolated_db, asyncio.get_running_loop())
    monkeypatch.setattr(database, "db_job_boundary", boundary)
    tool = ProbeTool()
    boundary.task = asyncio.create_task(
        execute_as(registry_of(tool), identity, thread_id, tool.name),
    )

    with pytest.raises(asyncio.CancelledError):
        await boundary.task

    assert boundary.fired
    assert tool.calls == 0
    [dispatch] = dispatch_rows(get_db())
    assert dispatch["attempt_id"] == attempt_id
    assert dispatch["settled_at"] is not None
    assert dispatch["outcome"] == "unknown"
