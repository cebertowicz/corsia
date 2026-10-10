"""Validation and immutable subscription admission for detached inference."""
from __future__ import annotations

import json
from dataclasses import replace

from connections.account_policy import require_launch


def validate_request(system_prompt, user_prompt, output_schema, max_output_tokens, timeout_seconds):
    from llm.isolated_structured import _strict_json_loads

    if not isinstance(system_prompt, str) or not system_prompt.strip():
        raise ValueError("isolated system_prompt must not be blank")
    if not isinstance(user_prompt, str) or not user_prompt.strip():
        raise ValueError("isolated user_prompt must not be blank")
    if not isinstance(output_schema, dict):
        raise ValueError("isolated output_schema must be an object")
    if isinstance(max_output_tokens, bool) or not isinstance(max_output_tokens, int) or max_output_tokens <= 0:
        raise ValueError("isolated max_output_tokens must be positive")
    if isinstance(timeout_seconds, bool) or not isinstance(timeout_seconds, (int, float)) or timeout_seconds <= 0:
        raise ValueError("isolated timeout_seconds must be positive")
    try:
        frozen = _strict_json_loads(json.dumps(output_schema, sort_keys=True, separators=(",", ":"),
                                              ensure_ascii=False, allow_nan=False))
    except (TypeError, ValueError, json.JSONDecodeError) as exc:
        raise ValueError("isolated output_schema must be strict JSON") from exc
    if not isinstance(frozen, dict):
        raise ValueError("isolated output_schema must be an object")
    return frozen


def admit_structured(target, request):
    from connections.account_operation import admit_operation

    return admit_operation(target.harness, target.catalog_id, request, actor_thread_id=request.actor_thread_id)


def require_structured_write(target, request):
    from connections.account_operation import require_operation

    return require_operation(target.harness, target.catalog_id, request, actor_thread_id=request.actor_thread_id)


def new_codex_invocation_session(target, request, capture, *, binary, working_dir,
                                 codex_home, session_factory, config_overrides):
    session = session_factory(
        binary=binary,
        task=None,
        codex_thread_id=None,
        working_dir=str(working_dir),
        model=target.transport_model,
        effort=request.reasoning_effort,
        history_items=(),
        dynamic_tools=(),
        tool_registry=None,
        corsia_thread_id=request.thread_id,
        project_id=None,
        sandbox_mode="read-only",
        approval_policy="never",
        ephemeral=True,
        allow_provider_model_fallback=False,
        developer_instructions=request.system_prompt,
        codex_home=str(codex_home),
        config_overrides=config_overrides,
        on_event=capture.on_event,
        on_exit=capture.on_exit,
        on_raw_message=capture.on_raw_message,
    )
    from connections.account_operation import bind_operation_session

    bind_operation_session(request, session, kind='detached', model=target.catalog_id)
    session.vendor_actor_thread_id = request.actor_thread_id
    return session
