"""Accept and render checkpoint output without judging its shape."""

from __future__ import annotations

import asyncio
import os
from copy import deepcopy
from dataclasses import dataclass
from typing import Any

from core.checkpoint_cold_count import cold_input_tokens
from core.checkpoint_resume import CheckpointAdmission
from core.checkpoint_snapshot_render import (
    render_artifact_snapshot,
    render_checkpoint_dates,
    render_plan_status_snapshot,
)
from core.checkpoint_types import (
    CHECKPOINT_CONTEXT_TRUNCATED,
    CHECKPOINT_OUTPUT_TOKENS,
    CheckpointRecord,
)
from core.checkpoint_warm_request import warm_capture_refused
from core.continuity_render import CONTINUITY_ENTRIES_HEADER, render_continuity_entries


# Leave room for provider framing beyond the conservative cold payload count.
COLD_FIT_FRAMING_MARGIN_DIVISOR = 20


@dataclass(frozen=True, slots=True)
class CapturedCheckpoint:
    """The only provider output facts that checkpoint acceptance owns."""

    raw_body: str
    has_staged_edits: bool
    usage: dict[str, Any] | None

    def usable_result(self) -> bool:
        """Whether this call's narrative has text to install as covered history."""
        return usable_result(self.raw_body)


def usable_result(raw_body: str) -> bool:
    """Whether a narrative is nonblank enough to become the install source.

    Staged continuity edits alone are not sufficient: an installed checkpoint
    replaces the raw archive as the reconstruction source (#1758), so a blank
    narrative would silently discard the history it claims to cover (#1767).
    """
    return bool(raw_body.strip())


def carry_partial_narrative(previous: str, partial: str) -> str:
    """Keep prior narrative when a cut-off update cannot replace it completely."""
    if not previous:
        return partial
    suffix = "\n\n[Incomplete checkpoint update]\n" + partial if partial.strip() else ""
    return previous + suffix


def render_installed_checkpoint(checkpoint: CheckpointRecord) -> str:
    """Render the exact body beside the exact installed continuity snapshot."""
    raw_body = checkpoint.raw_result or ""
    snapshot = checkpoint.continuity_snapshot_json or "[]"
    warning = (
        "[CORSIA warning: Checkpoint truncated at the context limit. The update "
        "is incomplete; completed continuity edits were retained. Do not treat "
        "omitted work as completed.]\n"
        if checkpoint.error == CHECKPOINT_CONTEXT_TRUNCATED else ""
    )
    rendered = (
        warning
        + render_checkpoint_dates(checkpoint.through_at, checkpoint.captured_at)
        + "[CORSIA checkpoint narrative]\n"
        + raw_body
        + "\n" + CONTINUITY_ENTRIES_HEADER
        + render_continuity_entries(snapshot)
    )
    # Frozen at install (#1602); a NULL column was never captured, so its
    # section is absent and the pre-change prefix bytes are unchanged.
    for section in (
        render_artifact_snapshot(checkpoint.artifact_snapshot_json),
        render_plan_status_snapshot(checkpoint.plan_status_snapshot_json),
    ):
        if section:
            rendered += "\n" + section
    return rendered


def checkpoint_binary(target, db) -> str | None:
    """Resolve the ordinary binary for one selected checkpoint harness."""
    if target is None or target.harness == "native":
        return None
    if target.harness == "claude-code":
        from connections.models import get_connection

        configured = get_connection("claude-code", db=db) or {}
        value = os.environ.get("CLAUDE_BIN") or (configured.get("config") or {}).get(
            "binary"
        )
        return str(value or "claude")
    if target.harness == "codex":
        from subagents.codex_runtime_config import codex_binary, codex_connection

        return str(codex_binary(codex_connection(db)))
    raise ValueError(f"unsupported checkpoint harness: {target.harness}")


async def capture_warm_checkpoint_source(
    thread_id: str,
    boundary,
    *,
    target,
    binary: str | None,
    reasoning_effort: str | None,
    live: Any | None = None,
    prefix_bytes: int | None = None,
):
    """Capture one warm prefix through the selected harness adapter."""
    if target is None:
        return None
    harness = target.harness
    if harness not in {"claude-code", "codex"}:
        return warm_capture_refused(thread_id, harness, "native_source_unsupported")
    if binary is None:
        return warm_capture_refused(thread_id, harness, "no_binary")
    if harness == "claude-code":
        from core.checkpoint_cc_boundary import (
            capture_registered_boundary_warm_source,
        )

        warm_boundary = _durable_warm_boundary(thread_id, boundary, harness)
        if warm_boundary is None:
            return warm_capture_refused(thread_id, harness, "no_warm_boundary")
        return await capture_registered_boundary_warm_source(
            thread_id,
            boundary,
            warm_boundary,
            target=target,
            binary=binary,
            live=live,
        )
    from core.checkpoint_codex_fork import (
        capture_warm_source,
        warm_tool_boundary_prefix_bytes,
    )

    if prefix_bytes is None:
        warm_boundary = _durable_warm_boundary(thread_id, boundary, harness)
        if warm_boundary is None:
            return warm_capture_refused(thread_id, harness, "no_warm_boundary")
        prefix_bytes = await asyncio.to_thread(
            warm_tool_boundary_prefix_bytes,
            thread_id,
            warm_boundary,
        )
        if isinstance(prefix_bytes, str):
            return warm_capture_refused(thread_id, harness, prefix_bytes)
    return await capture_warm_source(
        thread_id,
        boundary,
        target=target,
        binary=binary,
        prefix_bytes=prefix_bytes,
    )


def _durable_warm_boundary(thread_id: str, boundary, harness: str):
    """Project the exact canonical row/group paired with a provider prefix."""
    from history.projection import render_portable_history
    from infra.database import get_db
    from subagents.harness_checkpoint_context import _warm_boundary

    db = get_db()
    try:
        projection = render_portable_history(
            thread_id,
            db=db,
            include_unfinished_turn=True,
        )
    finally:
        db.close()
    if harness not in {"claude-code", "codex"}:
        return None
    return _warm_boundary(projection, boundary)


def _cold_prior_narrative(checkpoint: CheckpointRecord) -> str:
    """Carry prior prose and truncation state without copying continuity bodies."""
    warning = (
        "[CORSIA warning: Prior checkpoint was truncated at the context limit.]\n"
        if checkpoint.error == CHECKPOINT_CONTEXT_TRUNCATED else ""
    )
    return warning + "[CORSIA prior checkpoint narrative]\n" + (checkpoint.raw_result or "")


def _cold_source(db, *, thread_id: str, projection, previous, boundary):
    from core.checkpoint_resume import installed_replay
    from history.projection import slice_portable_projection

    source = projection
    protected: list[dict[str, str]] = []
    if previous is not None:
        source = slice_portable_projection(
            installed_replay(previous, db=db).tail,
            through=boundary,
        )
        protected.append({
            "role": "user",
            "content": _cold_prior_narrative(previous),
        })
    return source, tuple(protected)


def _codex_cold_surface(
    working_dir: str, *, writer: object, registry=None, definitions=None,
    developer=None,
):
    """Freeze and count the exact known surface before the cold generation."""
    from subagents.codex_app_server_events import to_dynamic_tool_specs
    from subagents.harness_tool_bridge import (
        build_tool_registry,
        dynamic_tool_definitions,
    )
    from subagents.managed_launch import (
        codex_developer_instructions_with_write_path,
        codex_tools_are_write_capable,
    )

    registry = registry if registry is not None else build_tool_registry(working_dir)
    if definitions is None:
        definitions = dynamic_tool_definitions(registry)
    dynamic_tool_specs = tuple(deepcopy(to_dynamic_tool_specs(definitions)))
    names = {str(definition.get("name") or "") for definition in dynamic_tool_specs}
    developer = codex_developer_instructions_with_write_path(
        developer,
        write_capable=codex_tools_are_write_capable(names),
        read_only=True,
    )
    payload: dict[str, object] = {"dynamicTools": dynamic_tool_specs}
    if developer is not None:
        payload["developerInstructions"] = developer
    return (
        cold_input_tokens(payload, writer),
        registry,
        dynamic_tool_specs,
        developer,
    )


def _cold_input_allowance(
    window: int | None, output_tokens: int, round_trip_tokens: int = 0,
) -> int:
    from core.checkpoint_fit import ColdFoldInputTooLarge

    if isinstance(window, bool) or not isinstance(window, int) or window <= 0:
        raise ColdFoldInputTooLarge("cold checkpoint has no proved context window")
    if (
        isinstance(output_tokens, bool)
        or not isinstance(output_tokens, int)
        or output_tokens <= 0
    ):
        raise ValueError("checkpoint output allowance must be a positive integer")
    allowance = (
        window - output_tokens - round_trip_tokens
        - window // COLD_FIT_FRAMING_MARGIN_DIVISOR
    )
    if allowance <= 0:
        raise ColdFoldInputTooLarge("checkpoint output and framing leave no input room")
    return allowance


def require_cold_checkpoint_room(
    window: int | None, instruction: str, *, writer: object, protected=(),
    output_tokens: int = CHECKPOINT_OUTPUT_TOKENS,
) -> None:
    """Refuse a known impossible minimum without loading any provider surface."""
    from core.checkpoint_fit import ColdFoldInputTooLarge

    minimum = cold_input_tokens(
        ({"role": "system", "content": instruction}, *protected), writer,
    )
    if minimum > _cold_input_allowance(window, output_tokens):
        raise ColdFoldInputTooLarge("protected checkpoint input leaves no writer room")


def fit_cold_checkpoint_input(
    request, *, provider_surface_tokens: int, round_trip_tokens: int = 0,
):
    """Fit the frozen source once, using the surface loaded by its writer.

    ``round_trip_tokens`` keeps that much room free for a fork that runs its own
    tool loop: its first reply is appended to the input before the next call.
    """
    from core.checkpoint_fit import render_cold_fold_input

    source = request.cold_source
    if source is None or request.warm_source is not None:
        raise TypeError("cold fit requires one cold source and no warm source")
    if (
        isinstance(provider_surface_tokens, bool)
        or not isinstance(provider_surface_tokens, int)
        or provider_surface_tokens < 0
    ):
        raise ValueError("cold writer surface must be a non-negative integer")

    def count(messages) -> int:
        return provider_surface_tokens + cold_input_tokens(
            ({"role": "system", "content": request.instruction}, *messages),
            request.target,
        )

    return render_cold_fold_input(
        source.projection,
        max_input_tokens=_cold_input_allowance(
            request.target.context_window, request.max_output_tokens,
            round_trip_tokens,
        ),
        protected_blocks=source.protected_blocks,
        count_tokens=count,
    )


def build_cold_checkpoint_admission(
    *,
    checkpoint_id: str,
    thread_id: str,
    trigger: str,
    source_target,
    source_model: str | None = None,
    writer_target,
    boundary,
    previous,
    projection,
    working_dir: str,
    reasoning_effort: str | None,
    binary: str | None,
    mcp_config: str | None = None,
    originating_turn_id: str | None = None,
    db,
) -> CheckpointAdmission:
    """Freeze one cold source; its existing writer owns final surface admission."""
    from core.checkpoint_types import (
        CheckpointRequest, ColdCheckpointSource, ProviderCheckpointRequest,
    )
    from infra.time import utc_now_iso
    from prompts.checkpoint import build_checkpoint_system_prompt

    instruction = build_checkpoint_system_prompt()
    source, protected = _cold_source(
        db,
        thread_id=thread_id,
        projection=projection,
        previous=previous,
        boundary=boundary,
    )
    require_cold_checkpoint_room(
        writer_target.context_window, instruction, writer=writer_target,
        protected=protected,
    )
    source_harness = getattr(source_target, "harness", None) or "unknown"
    source_model = (
        source_model
        or getattr(source_target, "catalog_id", None)
        or "unknown"
    )
    request = CheckpointRequest(
        id=checkpoint_id,
        thread_id=thread_id,
        history_epoch=boundary.history_epoch,
        previous_generation_id=previous.id if previous else None,
        through_seq=boundary.seq,
        through_message_id=boundary.message_id,
        trigger=trigger,
        mode="cold",
        source_harness=source_harness,
        source_model=source_model,
        writer_harness=writer_target.harness,
        writer_model=writer_target.catalog_id,
        originating_turn_id=originating_turn_id,
        first_included_seq=None,
        started_at=utc_now_iso(),
    )
    provider = ProviderCheckpointRequest(
        checkpoint_id=checkpoint_id,
        thread_id=thread_id,
        target=writer_target,
        instruction=instruction,
        cold_source=ColdCheckpointSource(source, protected),
        reasoning_effort=reasoning_effort,
        working_dir=working_dir,
        binary=binary,
        mcp_config=mcp_config,
    )
    return CheckpointAdmission(request, provider)
