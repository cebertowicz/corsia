"""Agent model-selection control using the canonical FIFO switch owner."""
from __future__ import annotations

from functools import partial
from typing import Any

from infra.db_async import run_in_db
from tools.base import Tool


class DelegateSetModelTool(Tool):
    """Switch a delegated thread's selected model."""

    @property
    def name(self) -> str:
        return "delegate_set_model"

    @property
    def description(self) -> str:
        return (
            "Change a running thread's model or harness in place, for any reason "
            "(upgrade a struggling worker, route around an outage, cut cost); the thread "
            "keeps its history, so prefer this to replacing it. It does not send a turn. "
            "A history too large for the target is carried over by a checkpoint on the "
            "current model; set checkpoint_model=target to use the target model instead."
        )

    @property
    def parameters(self) -> dict[str, Any]:
        from tools.delegation import _tool_schema

        return _tool_schema({
            "thread_id": {"type": "string", "description": "Target thread id"},
            "model": {"type": "string", "description": "Explicit dispatchable target model"},
            "account_id": {"type": ["string", "null"], "description": "Optional permitted vendor account; changing an existing binding requires the idle account control."},
            "reason": {"type": ["string", "null"], "description": "Audit reason"},
            "checkpoint_model": {
                "type": ["string", "null"], "enum": ["target", None],
                "description": "Optional explicit target-model checkpoint override; omit to use the current model",
            },
        }, ["thread_id", "model"])

    async def execute(self, thread_id: str, model: str, reason: str | None = None, **kwargs: Any) -> str:
        from infra.local_only import LocalityRefused
        from tools import delegation
        from thread.model import switch_model
        from thread.model_state import ModelSwitchConflict, ModelSwitchOutcome, ModelSwitchPreparationError

        context = delegation._require_context(kwargs)
        if isinstance(context, str):
            return context
        from_thread_id, current = context
        new_model = model.strip()
        if not new_model:
            return "Error: model is required; there is no silent default."
        exists = await run_in_db(delegation._delegation_target_exists, thread_id, current["user_id"])
        # Read on the account the target thread runs on, or the one it is moving to.
        new_model, refusal = await run_in_db(partial(
            delegation.eligible_delegation_model, new_model, thread_id=thread_id if exists else None,
            account_id=kwargs.get("account_id")))
        if refusal:
            return f"Error: {refusal}"
        checkpoint_override = kwargs.get("checkpoint_model")
        if checkpoint_override is not None and checkpoint_override != "target":
            return "Error: checkpoint_model may be omitted (current) or set to target."
        switch_reason = (reason or "").strip() or "delegation-tool"
        if not exists:
            return f"Error: Thread not found: {thread_id}"
        try:
            result = await switch_model(
                thread_id, new_model, user_id=current["user_id"], actor_kind="agent",
                actor_id=from_thread_id, reason=switch_reason, checkpoint_override=checkpoint_override,
                **({"account_id": kwargs["account_id"]} if kwargs.get("account_id") is not None else {}),
            )
        except ModelSwitchConflict as exc:
            return f"Error: model switch conflict: {exc}"
        except (LocalityRefused, ModelSwitchPreparationError) as exc:
            return f"Error: model switch rejected: {exc}"
        if not isinstance(result, ModelSwitchOutcome):
            return delegation._delegate_pending_switch(thread_id, new_model, checkpoint_override, result)
        return delegation._delegate_committed_switch(thread_id, result)
