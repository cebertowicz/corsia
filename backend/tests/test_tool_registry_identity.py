from __future__ import annotations

from contextlib import contextmanager
import json
import sqlite3
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock

import pytest

from tools.base import Tool
from tools.registry import ToolRegistry


class _CaptureTool(Tool):
    def __init__(self) -> None:
        self.calls: list[dict[str, Any]] = []

    @property
    def name(self) -> str:
        return "capture"

    @property
    def description(self) -> str:
        return "Capture the exact backend execution context."

    @property
    def parameters(self) -> dict[str, Any]:
        return {
            "type": "object",
            "properties": {
                "value": {"type": "string"},
                "payload": {"type": "object"},
            },
            "required": ["value"],
            "additionalProperties": False,
        }

    async def execute(self, **kwargs: Any) -> str:
        self.calls.append(kwargs)
        return "ok"


class _ResultTool(Tool):
    def __init__(
        self,
        name: str,
        *,
        result: str = "",
        error: Exception | None = None,
    ) -> None:
        self._name = name
        self._result = result
        self._error = error

    @property
    def name(self) -> str:
        return self._name

    @property
    def description(self) -> str:
        return "Return one deterministic test result."

    @property
    def parameters(self) -> dict[str, Any]:
        return {
            "type": "object",
            "properties": {},
            "additionalProperties": False,
        }

    async def execute(self, **_kwargs: Any) -> str:
        if self._error is not None:
            raise self._error
        return self._result


class _TransitionOwningProjectCreateTool(_ResultTool):
    def __init__(self) -> None:
        super().__init__("project_create")

    async def execute(self, **_kwargs: Any) -> str:
        from infra.repo_policy_transition import repo_policy_transition

        with repo_policy_transition(active_process_count=lambda: 0):
            return "ok"


def _seed_session(
    db_path,
    *,
    thread_id: str,
    user_id: str,
    project_id: str | None,
) -> None:
    db = sqlite3.connect(db_path)
    db.execute(
        "INSERT INTO threads "
        "(id,user_id,model,project_id,created_at,updated_at) "
        "VALUES (?,?,?,?,?,?)",
        (thread_id, user_id, "test/model", project_id, "t", "t"),
    )
    db.commit()
    db.close()


@pytest.mark.asyncio
async def test_registry_rejects_execution_without_task_local_session():
    from infra import state

    registry = ToolRegistry()
    tool = _CaptureTool()
    registry.register(tool)
    token = state._current_thread_id.set(None)
    try:
        result = await registry.execute("capture", {"value": "safe"})
    finally:
        state._current_thread_id.reset(token)

    assert result.startswith(
        "Error: Tool execution requires an exact current session identity."
    )
    assert tool.calls == []


@pytest.mark.asyncio
async def test_registry_validates_sanitized_public_arguments_before_db_lookup(
    monkeypatch,
):
    registry = ToolRegistry()
    registry.register(_CaptureTool())
    lookup = AsyncMock()
    monkeypatch.setattr(registry, "_load_thread_context", lookup)

    result = await registry.execute(
        "capture",
        {"_thread_id": "forged", "unexpected": "value"},
    )

    assert result.startswith("Error: Invalid parameters for tool 'capture':")
    assert "_thread_id" not in result
    lookup.assert_not_awaited()


@pytest.mark.asyncio
async def test_registry_rejects_task_local_session_missing_from_database(
    isolated_db,
):
    from infra import state

    registry = ToolRegistry()
    tool = _CaptureTool()
    registry.register(tool)
    token = state._current_thread_id.set("missing-thread")
    try:
        result = await registry.execute("capture", {"value": "safe"})
    finally:
        state._current_thread_id.reset(token)

    assert result.startswith(
        "Error: Tool execution session identity is unavailable."
    )
    assert tool.calls == []


@pytest.mark.asyncio
async def test_registry_blocks_tool_effects_while_restart_drain_is_active(
    isolated_db,
):
    from automation import self_restart
    from infra import state

    _seed_session(
        isolated_db,
        thread_id="draining-thread",
        user_id="draining-user",
        project_id=None,
    )
    registry = ToolRegistry()
    tool = _CaptureTool()
    registry.register(tool)
    token = state._current_thread_id.set("draining-thread")
    operation_id = self_restart.try_acquire(
        "draining-thread", "draining-user", "sha", "dev", "test",
    )
    assert operation_id is not None
    assert self_restart.transition_operation(
        operation_id, expected="holding", target="steering",
    )
    try:
        result = await registry.execute("capture", {"value": "must-not-run"})
    finally:
        state._current_thread_id.reset(token)

    assert result == (
        "Error: CORSIA is restarting. Until it is back, every tool call except "
        "restart_resume is refused, so retrying or switching tools will not help. "
        "If unfinished work must continue after startup, call restart_resume now "
        "(load it with tools_load if it is not in your tool list); it will wake this "
        "thread when CORSIA is back. Otherwise end your turn."
    )
    assert "Analyze the error above" not in result
    assert tool.calls == []


@pytest.mark.asyncio
async def test_registry_allows_only_exact_turn_restart_resume_during_drain(
    isolated_db,
):
    from automation import self_restart
    from infra import state
    from infra.database import get_db
    from infra.thread_driver import admit_input
    from tools.restart import RestartResumeTool
    from tools.tool_surface import ToolCallTool, ToolsLoadTool

    thread_id = "restart-resume-thread"
    turn_id = "restart-resume-turn"
    _seed_session(
        isolated_db,
        thread_id=thread_id,
        user_id="restart-resume-user",
        project_id=None,
    )
    db = get_db()
    try:
        admission = admit_input(
            thread_id,
            role="user",
            content="continue after restart",
            intent="apply_now",
            message_id="restart-resume-input",
            turn_id=turn_id,
            boot_epoch="test-boot",
            admitted_at="t",
            db=db,
        )
        assert admission.batch is not None
        identity = admission.batch.identity
    finally:
        db.close()
    operation_id = self_restart.try_acquire(
        thread_id, "restart-resume-user", "sha", "dev", "test",
    )
    assert operation_id is not None
    assert self_restart.transition_operation(
        operation_id, expected="holding", target="steering",
    )

    registry = ToolRegistry()
    registry.register(RestartResumeTool())
    registry.register(ToolsLoadTool(registry))
    registry.register(ToolCallTool())
    registry.register(_ResultTool("other"))
    thread_token = state._current_thread_id.set(thread_id)
    turn_token = state._current_turn_identity.set(identity)
    previous_drain = state._drain
    state._drain = {
        "reason": "release",
        "started_at": 0,
    }
    try:
        loaded = await registry.execute("tools_load", {"names": ["restart_resume"]})
        first = json.loads(await registry.execute(
            "tool_call", {"name": "restart_resume", "arguments": {}},
        ))
        second = json.loads(await registry.execute("restart_resume", {}))
        blocked = await registry.execute("other", {})
    finally:
        state._drain = previous_drain
        state._current_turn_identity.reset(turn_token)
        state._current_thread_id.reset(thread_token)

    assert loaded.startswith("Loaded 1 tool(s).")
    assert first == second
    assert first["ok"] is True
    assert blocked.startswith("Error: CORSIA is restarting.")
    assert first["status"] == "registered"
    assert first["turn_id"] == turn_id
    control_id = first["registration_id"]
    assert isinstance(control_id, str) and control_id
    db = sqlite3.connect(isolated_db)
    try:
        row = db.execute(
            "SELECT status,COUNT(*) FROM restart_wake_registrations "
            "WHERE operation_id=? AND thread_id=? AND turn_id=?",
            (operation_id, thread_id, turn_id),
        ).fetchone()
        assert row == ("pending", 1)
        rows = db.execute(
            "SELECT metadata FROM messages WHERE thread_id=? AND role='system'",
            (thread_id,),
        ).fetchall()
        assert len(rows) == 1
        assert json.loads(rows[0][0])["event_type"] == "context.digest"
    finally:
        db.close()


@pytest.mark.asyncio
async def test_registry_discards_forged_context_and_injects_exact_db_identity(
    isolated_db,
):
    from infra import state

    _seed_session(
        isolated_db,
        thread_id="real-thread",
        user_id="real-user",
        project_id="real-project",
    )
    registry = ToolRegistry()
    tool = _CaptureTool()
    registry.register(tool)
    supplied = {
        "value": "safe",
        "_thread_id": "forged-thread",
        "_thread_user_id": "forged-user",
        "_thread_project_id": "forged-project",
        "_host_access_lease_id": "forged-lease",
        "_host_access_turn_identity": "forged-turn",
        "_lease_permit": "forged-permit",
    }

    token = state._current_thread_id.set("real-thread")
    try:
        result = await registry.execute("capture", supplied)
    finally:
        state._current_thread_id.reset(token)

    assert result == "ok"
    assert supplied["_thread_id"] == "forged-thread"
    assert tool.calls == [{
        "value": "safe",
        "_thread_id": "real-thread",
        "_thread_project_id": "real-project",
    }]


@pytest.mark.asyncio
async def test_registry_copies_nested_provider_arguments_before_execution(
    isolated_db,
):
    from infra import state

    _seed_session(
        isolated_db,
        thread_id="copy-thread",
        user_id="copy-user",
        project_id=None,
    )
    registry = ToolRegistry()
    tool = _CaptureTool()
    registry.register(tool)
    supplied = {
        "value": "safe",
        "payload": {"nested": ["original"]},
    }

    token = state._current_thread_id.set("copy-thread")
    try:
        assert await registry.execute("capture", supplied) == "ok"
    finally:
        state._current_thread_id.reset(token)

    tool.calls[0]["payload"]["nested"].append("tool-mutation")
    assert supplied == {
        "value": "safe",
        "payload": {"nested": ["original"]},
    }


@pytest.mark.asyncio
async def test_registry_host_access_request_keeps_user_identity_out_of_tool_kwargs(
    isolated_db,
    monkeypatch,
):
    from infra import state
    from tools import host_access

    _seed_session(
        isolated_db,
        thread_id="lease-thread",
        user_id="lease-user",
        project_id="lease-project",
    )
    captured: list[tuple[str, dict[str, Any]]] = []

    async def request(thread_id: str, **kwargs: Any) -> dict[str, Any]:
        captured.append((thread_id, kwargs))
        return {"state": "awaiting_user"}

    monkeypatch.setattr(host_access, "request_reviewed_lease", request)
    registry = ToolRegistry()
    registry.register(host_access.RequestHostAccessTool())
    token = state._current_thread_id.set("lease-thread")
    try:
        result = await registry.execute(
            "request_host_access",
            {
                "purpose": "Inspect the host service.",
                "plan": ["Read service status."],
                "duration_seconds": 60,
            },
        )
    finally:
        state._current_thread_id.reset(token)

    assert '"state":"awaiting_user"' in result
    assert captured == [
        (
            "lease-thread",
            {
                "purpose": "Inspect the host service.",
                "plan": ["Read service status."],
                "duration_seconds": 60,
            },
        )
    ]


@pytest.mark.asyncio
async def test_registry_enters_host_gate_with_exact_identity_and_scopes_result(
    isolated_db,
    monkeypatch,
):
    from infra import state
    from infra import repo_policy_transition
    from tools import host_access_gate

    _seed_session(
        isolated_db,
        thread_id="gated-thread",
        user_id="gated-user",
        project_id="gated-project",
    )
    registry = ToolRegistry()
    tool = _CaptureTool()
    registry.register(tool)
    events: list[tuple[str, object]] = []
    authorization = host_access_gate.HostActionAuthorization(
        _parameter_overrides=(("value", "reviewed"),),
    )

    async def authorize(**kwargs):
        events.append(("authorize", kwargs))
        return authorization

    @contextmanager
    def permit_scope(value):
        events.append(("permit-enter", value))
        try:
            yield
        finally:
            events.append(("permit-exit", value))

    @contextmanager
    def repo_scope():
        events.append(("repo-enter", None))
        try:
            yield
        finally:
            events.append(("repo-exit", None))

    monkeypatch.setattr(host_access_gate, "authorize_host_action", authorize)
    monkeypatch.setattr(
        host_access_gate,
        "host_action_permit_scope",
        permit_scope,
    )
    monkeypatch.setattr(
        repo_policy_transition,
        "admit_repo_effect",
        repo_scope,
    )
    token = state._current_thread_id.set("gated-thread")
    try:
        result = await registry.execute("capture", {"value": "requested"})
    finally:
        state._current_thread_id.reset(token)

    assert result == "ok"
    assert [event[0] for event in events] == [
        "authorize",
        "repo-enter",
        "permit-enter",
        "permit-exit",
        "repo-exit",
    ]
    gate_call = events[0][1]
    assert gate_call == {
        "thread_id": "gated-thread",
        "project_id": "gated-project",
        "tool": tool,
        "tool_name": "capture",
        "params": {"value": "requested"},
    }
    assert tool.calls == [
        {
            "value": "reviewed",
            "_thread_id": "gated-thread",
            "_thread_project_id": "gated-project",
        }
    ]


async def _execute_result_tool(
    isolated_db,
    monkeypatch,
    *,
    tool: Tool,
    params: dict[str, Any] | None = None,
) -> str:
    from infra import state
    from tools import host_access_gate

    _seed_session(
        isolated_db,
        thread_id="result-thread",
        user_id="result-user",
        project_id=None,
    )

    async def authorize(**_kwargs):
        return host_access_gate.HostActionAuthorization()

    monkeypatch.setattr(host_access_gate, "authorize_host_action", authorize)
    registry = ToolRegistry()
    registry.register(tool)
    token = state._current_thread_id.set("result-thread")
    try:
        return await registry.execute(tool.name, params or {})
    finally:
        state._current_thread_id.reset(token)


@pytest.mark.asyncio
async def test_project_create_owns_repo_transition_without_registry_self_rejection(
    isolated_db,
    monkeypatch,
):
    result = await _execute_result_tool(
        isolated_db,
        monkeypatch,
        tool=_TransitionOwningProjectCreateTool(),
    )

    assert result == "ok"


@pytest.mark.parametrize("tool_name", ["read_file", "grep", "bash"])
@pytest.mark.asyncio
async def test_registry_sanitizes_recognized_credentials_in_tool_results(
    isolated_db,
    monkeypatch,
    tool_name,
):
    result = await _execute_result_tool(
        isolated_db,
        monkeypatch,
        tool=_ResultTool(
            tool_name,
            result=(
                "service status: ready\n"
                "Authorization: Bearer model-visible-secret\n"
                "postgresql://alice:dsn-secret@db.example/app\n"
                "machine github.com login alice password netrc-secret\n"
                "oauth_token ghp_result_secret\n"
                "Authorization Bearer auth-result-secret"
            ),
        ),
    )

    for secret in (
        "model-visible-secret",
        "dsn-secret",
        "netrc-secret",
        "ghp_result_secret",
        "auth-result-secret",
    ):
        assert secret not in result
    assert "Authorization: Bearer [REDACTED]" in result
    assert "postgresql://alice:[REDACTED]@db.example/app" in result
    assert "password [REDACTED]" in result
    assert "oauth_token [REDACTED]" in result
    assert "Authorization Bearer [REDACTED]" in result
    assert "service status: ready" in result


@pytest.mark.parametrize("tool_name", ["read_file", "grep", "bash"])
@pytest.mark.asyncio
async def test_registry_sanitizes_http_userinfo_and_incomplete_private_keys(
    isolated_db,
    monkeypatch,
    tool_name,
):
    result = await _execute_result_tool(
        isolated_db,
        monkeypatch,
        tool=_ResultTool(
            tool_name,
            result=(
                "origin https://ghp_model_visible@github.com/acme/private.git\n"
                "fetch git+https://vcs_model_visible@example.test/repository\n"
                "-----BEGIN PRIVATE KEY-----\n"
                "partial-model-visible-key\n"
            ),
        ),
    )

    assert "ghp_model_visible" not in result
    assert "vcs_model_visible" not in result
    assert "partial-model-visible-key" not in result
    assert "https://[REDACTED]@github.com/acme/private.git" in result
    assert "git+https://[REDACTED]@example.test/repository" in result
    assert "-----BEGIN PRIVATE KEY-----\n[REDACTED]" in result


@pytest.mark.asyncio
async def test_registry_sanitizes_exception_text_before_return(
    isolated_db,
    monkeypatch,
):
    result = await _execute_result_tool(
        isolated_db,
        monkeypatch,
        tool=_ResultTool(
            "read_file",
            error=RuntimeError(
                "request failed with token=exception-secret"
            ),
        ),
    )

    assert "exception-secret" not in result
    assert "token=[REDACTED]" in result
    assert result.startswith("Error executing read_file:")


@pytest.mark.asyncio
async def test_registry_sanitizes_exception_telemetry(
    isolated_db,
    monkeypatch,
):
    captured_attributes: list[dict[str, Any]] = []

    def capture_attributes(_span, attributes):
        captured_attributes.append(attributes)

    monkeypatch.setattr("tools.registry.set_attrs", capture_attributes)
    result = await _execute_result_tool(
        isolated_db,
        monkeypatch,
        tool=_ResultTool(
            "read_file",
            error=RuntimeError("GITHUB_TOKEN=telemetry-secret"),
        ),
    )

    encoded_attributes = json.dumps(captured_attributes)
    assert "telemetry-secret" not in result
    assert "telemetry-secret" not in encoded_attributes
    assert "GITHUB_TOKEN=[REDACTED]" in encoded_attributes


@pytest.mark.asyncio
async def test_registry_sanitizes_managed_process_logs(
    isolated_db,
    monkeypatch,
):
    from pathlib import Path

    from host_access.processes import ProcessRecord
    from tools.managed_processes import ManagedProcessLogsTool

    class _Owner:
        @staticmethod
        def read_logs(*_args, **_kwargs):
            return (
                "worker ready\n"
                "Cookie: session=managed-log-secret; theme=dark"
            )

        @staticmethod
        def status(handle, **_kwargs):
            return ProcessRecord(
                handle=handle,
                thread_id="thread",
                turn_id="turn",
                lease_id=None,
                lease_revision=None,
                permit_id=None,
                pid=1,
                pgid=1,
                creation_fingerprint="fingerprint",
                mode="background",
                log_path=Path("/nonexistent"),
                terminal_state="running",
            )

    result = await _execute_result_tool(
        isolated_db,
        monkeypatch,
        tool=ManagedProcessLogsTool(_Owner()),
        params={"handle": "opaque-handle"},
    )
    payload = json.loads(result)

    assert "managed-log-secret" not in result
    assert {key: payload[key] for key in ("handle", "logs", "state")} == {
        "handle": "opaque-handle",
        "logs": "worker ready\nCookie: session=[REDACTED]; theme=[REDACTED]",
        "state": "running",
    }


def test_repo_transition_owner_set_matches_the_tools_that_own_a_transition():
    """S5: the exemption is pinned to the tools, not to typed-in names.

    Renaming either tool, or pointing either at a different entry, breaks this
    before it can silently widen or narrow the repository fence.
    """
    from tools.project_repo_register import ProjectRepoRegisterTool
    from tools.projects import CreateProjectTool
    from tools.registry import _REPO_TRANSITION_OWNERS

    assert _REPO_TRANSITION_OWNERS == frozenset({
        CreateProjectTool().name, ProjectRepoRegisterTool().name,
    })
    # frozenset membership is exact: no prefix or category grant slips in.
    assert "project" not in _REPO_TRANSITION_OWNERS
    assert "project_repo_detach" not in _REPO_TRANSITION_OWNERS


@pytest.mark.parametrize("tool_name", ["project_create", "project_repo_register"])
@pytest.mark.asyncio
async def test_registry_skips_the_repo_effect_only_for_transition_owners(
    isolated_db, monkeypatch, tool_name,
):
    """An exempt tool enters its transition with no effect of its own held."""
    from infra import state
    from infra.repo_policy_transition import repo_policy_transition
    from tools import host_access_gate

    class _OwnTool(_ResultTool):
        def __init__(self) -> None:
            super().__init__(tool_name)

        async def execute(self, **_kwargs: Any) -> str:
            with repo_policy_transition(active_process_count=lambda: 0):
                return "ok"

    _seed_session(
        isolated_db, thread_id="owner-thread", user_id="owner-user", project_id=None,
    )

    async def authorize(**_kwargs):
        return host_access_gate.HostActionAuthorization()

    monkeypatch.setattr(host_access_gate, "authorize_host_action", authorize)
    registry = ToolRegistry()
    registry.register(_OwnTool())
    token = state._current_thread_id.set("owner-thread")
    try:
        assert await registry.execute(tool_name, {}) == "ok"
    finally:
        state._current_thread_id.reset(token)


@pytest.mark.asyncio
async def test_registry_keeps_repo_effect_exclusion_for_every_other_tool(
    isolated_db, monkeypatch,
):
    """A tool that merely takes an effect still cannot cross a transition."""
    from infra import state
    from infra.repo_policy_transition import repo_policy_transition
    from tools import host_access_gate

    _seed_session(
        isolated_db, thread_id="effect-thread", user_id="effect-user", project_id=None,
    )

    async def authorize(**_kwargs):
        return host_access_gate.HostActionAuthorization()

    monkeypatch.setattr(host_access_gate, "authorize_host_action", authorize)
    registry = ToolRegistry()
    registry.register(_ResultTool("capture"))

    with repo_policy_transition(active_process_count=lambda: 0):
        token = state._current_thread_id.set("effect-thread")
        try:
            result = await registry.execute("capture", {})
        finally:
            state._current_thread_id.reset(token)

    assert "Repository tool effect is blocked by an active policy transition." in result
