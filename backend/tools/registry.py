"""Tool registry for dynamic tool management."""

from contextlib import nullcontext
from dataclasses import replace
import logging
from typing import Any

from host_access.credential_sanitizer import sanitize_credential_text
from tools.result_sanitizer import sanitize_result
from infra.tool_result_errors import is_error_result
from tools.base import Tool
from tools.mcp_stamp import ConnectedAppResult, restamp, with_connected_app_stamp
from tools.post_update_reminder import remind_current_turn
from tools.recorded_call import CompletedToolFailure, execute_recorded_call
from tools.tool_execution_context import inject_context, mcp_reach_refusal, strip_reserved_context
from tools.tool_surface import (
    TOOL_CALL,
    TOOLS_LOAD,
    invalid_dispatch_hint,
    reachable_tool_names,
    resolve_dispatch,
    unknown_tool_error,
)

from eval.otel import get_tracer, set_attrs, SPAN_TOOL_CALL
from eval.tool_allowlist import record_only_result
_RESERVED_EXECUTION_PREFIXES = (
    "_thread_",
    "_host_access_",
    "_lease_",
    "_checkpoint_",
    "_review_",
)

# Tool names (resolved, post-dispatch) whose own execution enters
# ``infra.repo_policy_transition.repo_policy_transition``. They are excused
# from the registry's ``admit_repo_effect()`` wrapper because that effect would
# be the active effect their own transition refuses. Nothing else is exempt:
# effects, transitions and owned processes stay mutually exclusive.
_REPO_TRANSITION_OWNERS = frozenset({"project_create", "project_repo_register"})

log = logging.getLogger(__name__)


def _is_reserved_execution_param(key: object) -> bool:
    return isinstance(key, str) and key.startswith(_RESERVED_EXECUTION_PREFIXES)


class ToolRegistry:
    """Registry for agent tools."""

    def __init__(self) -> None:
        self._tools: dict[str, Tool] = {}

    def register(self, tool: Tool) -> None:
        """Register a tool."""
        self._tools[tool.name] = tool

    def unregister(self, name: str) -> None:
        """Unregister a tool by name."""
        self._tools.pop(name, None)

    def get(self, name: str) -> Tool | None:
        """Get a tool by name."""
        return self._tools.get(name)

    def has(self, name: str) -> bool:
        """Check if a tool is registered."""
        return name in self._tools

    def get_definitions(self) -> list[dict[str, Any]]:
        """Get all tool definitions in OpenAI format."""
        return [tool.to_schema() for tool in self._tools.values()]

    def retain_only(self, names: tuple[str, ...]) -> None:
        """Keep only an exact validated tool set for one disposable evaluation."""
        unknown = sorted(set(names) - set(self._tools))
        if unknown:
            raise ValueError("Unknown eval tool name(s): " + ", ".join(unknown))
        allowed = set(names)
        self._tools = {name: tool for name, tool in self._tools.items() if name in allowed}

    async def execute(
        self, name: str, params: dict[str, Any], *, review_request_id: str | None = None,
    ) -> str:
        """Execute a tool with optional backend-owned review scope."""
        try:
            result = sanitize_result(await self._execute_unredacted(
                name, params, review_request_id=review_request_id,
            ))
            if isinstance(result, ConnectedAppResult):
                from registry.tool_result_observation import observe_in_db
                result = await observe_in_db(result, resolve_dispatch(name, params)[1])
            # The reminder only touches plain text, so a stamped result earns it as text.
            plain = str(result) if isinstance(result, ConnectedAppResult) else result
            result = restamp(result, await remind_current_turn(plain, name, params))
        except CompletedToolFailure as failure:
            # Retained output answers the provider's own call: a dispatched
            # call is bound and replayed under ``tool_call``, not the tool it ran.
            dispatched = resolve_dispatch(name, params)[2] is not None
            failure.completed = tuple(
                replace(
                    call,
                    name=name if dispatched else call.name,
                    result=sanitize_result(call.result),
                )
                for call in failure.completed
            )
            raise
        return result

    async def _authorized_execute(
        self,
        tool: Any,
        name: str,
        model_params: dict[str, Any],
        execution_params: dict[str, Any],
        thread_context: dict[str, Any],
    ) -> Any:
        """Run the tool inside its host-action permit and repository policy.

        The call itself runs through ``tools.recorded_call``, which keeps its
        completed result, under the plan dispatch record and settlement that
        ``tools.dispatch_ownership`` owns.

        Raises:
            RuntimeError: The host-action gate tried to set reserved execution
                context, which only this registry may supply, or the call's
                plan dispatch could not be settled.
        """
        from infra.repo_policy_transition import admit_repo_effect
        import tools.confined_reads as confined_reads
        from tools.host_access_gate import (
            authorize_host_action,
            host_action_permit_scope,
        )

        if refusal := confined_reads.call_refusal(name, thread_context):
            return refusal
        authorization = await authorize_host_action(
            thread_id=thread_context["id"],
            project_id=thread_context.get("project_id"),
            tool=tool,
            tool_name=name,
            params=model_params,
        )
        parameter_overrides = authorization.parameter_overrides
        if any(_is_reserved_execution_param(key) for key in parameter_overrides):
            raise RuntimeError(
                "host-action gate returned reserved execution context"
            )
        execution_params.update(parameter_overrides)
        # These two tools own the stronger repository-policy transition for
        # the repository work they start, and they enter it themselves. The
        # transition already refuses while any effect is active, so admitting
        # this call's own effect would only make the tool collide with itself.
        # Every other tool is still admitted as an effect, and the transition
        # still excludes effects, other transitions and owned processes.
        repo_effect = (
            nullcontext()
            if name in _REPO_TRANSITION_OWNERS
            else admit_repo_effect()
        )
        with repo_effect, confined_reads.scope(thread_context):
            with host_action_permit_scope(authorization):
                return await execute_recorded_call(
                    tool, name, execution_params, thread_context,
                )

    @staticmethod
    def _is_drain_allowed(name: str, params: dict[str, Any]) -> bool:
        """Allow only the exact restart-continuation surface during a drain."""
        return name == "restart_resume" or (
            name == TOOLS_LOAD and params == {"names": ["restart_resume"]}
        )

    async def _restart_drain_refusal(
        self, name: str, params: dict[str, Any], thread_id: str,
    ) -> str | None:
        """Return the drain refusal unless this is exact restart continuation."""
        from automation.restart_messages import RESTART_DRAIN_REFUSAL
        from automation.self_restart import restart_holds_new_work
        from infra.db_async import run_in_db

        if not await run_in_db(restart_holds_new_work) or self._is_drain_allowed(name, params):
            return None
        log.info(
            "tool_registry: refused restart-drain tool call thread=%s tool=%s",
            thread_id, name,
        )
        return RESTART_DRAIN_REFUSAL

    def _callable(self, name: str, dispatcher: str | None) -> Tool | str:
        """Return the tool a call may run, or the error explaining why not.

        A dispatch reaches only what ``tools_load`` lists; a direct call by
        exact name keeps the registry's execute-by-name behavior.
        """
        tool = self._tools.get(name)
        if dispatcher is not None and name == TOOL_CALL and tool:
            return tool
        names = (
            reachable_tool_names(self, include_unreachable_mcp=True)
            if dispatcher else self.tool_names
        )
        if not tool or name not in names:
            return unknown_tool_error([name], names)
        return tool

    @staticmethod
    def _execution_params(model_params, thread_context, name, review_request_id):
        """Add trusted execution scope only after model arguments are validated."""
        execution_params = inject_context(model_params, thread_context, tool_name=name)
        if review_request_id is not None:
            execution_params["_review_request_id"] = review_request_id
        return execution_params

    async def _execute_unredacted(
        self, name: str, params: dict[str, Any], *, review_request_id: str | None = None,
    ) -> str:
        """Execute once, a ``tool_call`` as the tool it names; the boundary sanitizes."""
        name, params, dispatcher = resolve_dispatch(name, params)
        if (recorded := await record_only_result(name, params)) is not None:
            return recorded  # a carry-in sample's non-read-only call never reaches its tool

        _tracer = get_tracer("corsia.tools")
        with _tracer.start_as_current_span(SPAN_TOOL_CALL) as _tool_span:
            set_attrs(_tool_span, {
                "tool.name": name,
                "tool.is_null_probe": (name == "null_probe"),
                **({"tool.dispatcher": dispatcher} if dispatcher else {}),
            })

            tool = self._callable(name, dispatcher)
            if isinstance(tool, str):
                return tool
            if dispatcher is not None and name == TOOL_CALL:
                return await tool.execute(name=TOOL_CALL)

            # Stamped from the tool this call ran: every string it returns carries it.
            result = await self._run_tool(tool, name, params, dispatcher, review_request_id, _tool_span)
            return with_connected_app_stamp(tool, result)

    async def _run_tool(self, tool, name, params, dispatcher, review_request_id, _tool_span) -> str:
        """Validate, authorize and run one resolved tool; errors it does not author keep the hint."""
        _HINT = "\n\n[Analyze the error above and try a different approach.]"
        # A tool that states its own remedy gets no generic hint on the
        # errors it authors: its schema violations and its refusals name
        # the exact fix, and "try a different approach" contradicts them.
        # Errors this registry authors below - identity, drain, an
        # unhandled exception - are not the tool's, and keep the hint.
        _hint_suffix = "" if tool.self_describing_errors else _HINT
        try:
            model_params = tool.decode_string_arguments(
                strip_reserved_context(params, _is_reserved_execution_param)
            )
            errors = tool.validate_params(model_params)
            if errors:
                detail = invalid_dispatch_hint(tool) if dispatcher else ""
                return (
                    f"Error: Invalid parameters for tool '{name}': "
                    + "; ".join(errors) + detail + _hint_suffix
                )

            thread_context, identity_error = await self._load_thread_context()
            if identity_error:
                return identity_error + _HINT
            if thread_context is None:
                return (
                    "Error: Tool execution session identity is unavailable."
                    + _HINT
                )
            reach_refusal = await mcp_reach_refusal(tool, thread_context)
            if reach_refusal is not None:
                return reach_refusal
            execution_params = self._execution_params(
                model_params, thread_context, name, review_request_id,
            )

            drain_refusal = await self._restart_drain_refusal(
                name, model_params, thread_context["id"],
            )
            if drain_refusal is not None:
                return drain_refusal

            result = await self._authorized_execute(
                tool, name, model_params, execution_params, thread_context,
            )
            if is_error_result(result):
                return result + _hint_suffix
            set_attrs(_tool_span, {
                "tool.result_size_chars": len(result) if isinstance(result, str) else 0,
            })
            return result
        except CompletedToolFailure:
            raise
        except Exception as e:
            safe_error = sanitize_credential_text(str(e))
            set_attrs(_tool_span, {"tool.error": safe_error[:200]})
            return f"Error executing {name}: {safe_error}" + _HINT


    @staticmethod
    async def _load_thread_context() -> tuple[dict[str, Any] | None, str | None]:
        """Resolve exact task-local execution identity from the threads table."""
        from core.turn_core import TurnIdentity
        from infra import state

        thread_id = state._current_thread_id.get(None)
        if not isinstance(thread_id, str) or not thread_id:
            return (
                None,
                "Error: Tool execution requires an exact current session identity.",
            )
        turn_identity = state._current_turn_identity.get(None)
        if turn_identity is not None and not isinstance(
            turn_identity,
            TurnIdentity,
        ):
            return (
                None,
                "Error: Tool execution turn identity is invalid.",
            )

        def _load() -> tuple[dict[str, Any] | None, bool]:
            from infra.database import get_db

            db = get_db()
            try:
                row = db.execute(
                    "SELECT id,user_id,project_id,confinement,confined_read_dirs "
                    "FROM threads WHERE id=?",
                    (thread_id,),
                ).fetchone()
                context = dict(row) if row is not None else None
                if context is None or turn_identity is None:
                    return context, True
                active = db.execute(
                    "SELECT 1 FROM active_turns "
                    "WHERE id=? AND thread_id=? AND turn_order=? "
                    "AND status='running'",
                    (
                        turn_identity.turn_id,
                        thread_id,
                        turn_identity.turn_order,
                    ),
                ).fetchone()
                return context, active is not None
            finally:
                db.close()

        try:
            from infra.db_async import run_in_db

            context, turn_is_active = await run_in_db(_load)
        except Exception:
            log.exception(
                "tool execution identity lookup failed for session_id=%r",
                thread_id,
            )
            context = None
            turn_is_active = False
        if not context or not context.get("user_id"):
            return (
                None,
                "Error: Tool execution session identity is unavailable.",
            )
        if not turn_is_active:
            return (
                None,
                "Error: Tool execution turn identity is no longer active.",
            )
        if turn_identity is not None:
            context["_host_access_turn_identity"] = turn_identity
        return context, None


    @property
    def tool_names(self) -> list[str]:
        """Get list of registered tool names."""
        return list(self._tools.keys())

    def __len__(self) -> int:
        return len(self._tools)

    def __contains__(self, name: str) -> bool:
        return name in self._tools
