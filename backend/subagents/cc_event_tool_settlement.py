"""Durable settlement for Claude Code Assistant-Step tool ownership."""

from __future__ import annotations

import logging
from functools import partial

import subagents.cc_trust as cc_trust
from infra.delivery_states import (
    PROVIDER_MISSING_RESULT_REASON,
    UNDISPATCHED_TOOL_REASON,
)
from core.turn_core import TurnIdentity
from infra.db_async import run_in_db, run_in_db_unless_owned
# Registry observation is imported lazily at the non-gating seam below.
from registry.registration_writer import RegistrationOrigin
from tools.tool_display import SettledResult, tool_done_frame
from subagents.assistant_step_display import assistant_step_payload
from subagents.cc_runtime_state import Broadcast, _Live
from subagents.child_thread import (
    load_assistant_step_display,
    persist_tool_result,
)

log = logging.getLogger("subagents.cc_runtime_events")

PROVIDER_MISSING_TOOL_RESULT = (
    "Claude Code did not provide a result for this tool call. "
    "CORSIA will rebuild provider context before the next turn."
)
#: Sentinel owed-count meaning "we could not count, so stub every call this
#: Step still owns" — the pre-2026-08-15 behavior, kept for the unreadable
#: Step alone rather than for every Step.
_STUB_EVERY_DECLARED_CALL = 1_000_000


def turn_frame(identity: TurnIdentity, payload: dict) -> dict:
    return {**payload, **identity.frame_fields()}


async def emit_finalized_step(
    *, child_id: str, step_id: str, identity: TurnIdentity,
    bcast: Broadcast, db,
) -> None:
    step = await run_in_db_unless_owned(db, partial(
        load_assistant_step_display, child_id, step_id, db=db,
    ))
    calls = (step.get("metadata") or {}).get("tool_calls") or []
    bcast(child_id, turn_frame(identity, {
        "type": "step_text", "thread_id": child_id,
        **assistant_step_payload(step, message_id=step_id),
        "finalized_tool_ids": [
            call["id"] for call in calls
            if isinstance(call, dict) and isinstance(call.get("id"), str)
        ],
    }))


def _unheld_result_counts(
    child_id: str, *, step_ids: set[str], db,
) -> dict[str, int]:
    """Return each Step's result shortfall against durable non-synthetic rows.

    Declared minus held, floored at zero. Held means a durable tool row on that
    Step that CORSIA did not author as a stub, whether or not the provider has
    echoed it back yet. An undispatched call's authored row is its final answer,
    so it counts as held.
    """
    owns_db = db is None
    if owns_db:
        from infra.database import get_db

        db = get_db()
    try:
        owed: dict[str, int] = {}
        for step_id in step_ids:
            try:
                step = load_assistant_step_display(child_id, step_id, db=db)
                calls = (step.get("metadata") or {}).get("tool_calls") or []
                declared = sum(1 for call in calls if isinstance(call, dict))
                held = db.execute(
                    "SELECT COUNT(*) FROM messages WHERE thread_id=? AND role='tool' "
                    "AND json_valid(metadata) "
                    "AND json_extract(metadata,'$.assistant_step_id')=? "
                    "AND (COALESCE(json_extract(metadata,'$.synthetic_tool_result'),0)=0 "
                    "OR json_extract(metadata,'$.synthetic_reason')=?)",
                    (child_id, step_id, UNDISPATCHED_TOOL_REASON),
                ).fetchone()[0]
            except Exception:
                log.exception(
                    "cc settle: could not count held results for step %s on %s; "
                    "treating every declared call as unheld",
                    step_id, child_id,
                )
                owed[step_id] = _STUB_EVERY_DECLARED_CALL
                continue
            owed[step_id] = max(0, declared - int(held))
        return owed
    finally:
        if owns_db:
            db.close()


async def _settle_owner(
    *, live: _Live, child_id: str, identity: TurnIdentity,
    tool_id: str, step_id: str, bcast: Broadcast, db,
    content: str = PROVIDER_MISSING_TOOL_RESULT,
    reason: str = PROVIDER_MISSING_RESULT_REASON,
) -> str:
    name = live.tool_names.get(tool_id, "tool")
    await run_in_db_unless_owned(db, partial(
        persist_tool_result,
        child_id, turn_id=identity.turn_id, tool_use_id=tool_id,
        name=name, content=content, is_error=True,
        assistant_step_id=step_id, synthetic_tool_result=True,
        synthetic_reason=reason, db=db,
    ))
    live.tool_names.pop(tool_id, None)
    live.tool_step_ids.pop(tool_id, None)
    bcast(child_id, turn_frame(identity, tool_done_frame(
        child_id, tool_id, name,
        SettledResult(content, is_error=True, synthetic_reason=reason), content,
    )))
    return step_id


def _undispatched_tool_result(name: str) -> str:
    return (
        f"`{name}` is not a tool CORSIA serves, so nothing ran. "
        "Call CORSIA tools by their full `mcp__corsia__` name from your tool list."
    )


async def settle_undispatched_tool_calls(
    *, live: _Live, child_id: str, identity: TurnIdentity,
    step_id: str, tool_ids: list[str], bcast: Broadcast, db,
) -> None:
    """Answer one Step's calls that name a tool CORSIA does not serve.

    Claude Code runs only ``mcp__corsia__*`` tools
    (``cc_invocation.build_claude_cmd``), and ``on_init`` ends a process that
    declares anything else. A call to another name, or to a prefixed name the
    bridge never listed, never reaches the bridge, so CORSIA authors its final
    error result now. Claude Code's own refusal echo later
    binds to that row by provider call id. The caller owns ``live.event_lock``.
    """
    if not tool_ids:
        return
    for tool_id in tool_ids:
        await _settle_owner(
            live=live, child_id=child_id, identity=identity,
            tool_id=tool_id, step_id=step_id, bcast=bcast, db=db,
            content=_undispatched_tool_result(live.tool_names.get(tool_id, "tool")),
            reason=UNDISPATCHED_TOOL_REASON,
        )
    if not live.tool_step_ids:
        live.tools_settled.set()
    if step_id not in live.tool_step_ids.values():
        await emit_finalized_step(
            child_id=child_id, step_id=step_id, identity=identity,
            bcast=bcast, db=db,
        )


async def settle_missing_tool_results(
    *, live: _Live, child_id: str, identity: TurnIdentity,
    bcast: Broadcast, db,
) -> None:
    """Durably close unresolved CC calls before releasing their turn owner.

    The caller owns ``live.event_lock``. Removing ownership only after the
    idempotent result insert makes repeated terminal callbacks harmless without
    inventing a second lifecycle mechanism.

    "Unresolved" means CORSIA holds no result bytes, not merely that Claude
    Code has not echoed them. The tool ran here: ``execute_tool`` persists the
    exact bytes before returning them, while the provider echo only confirms
    that same output. Ownership uses Claude Code's ``tool_use_id``, which the
    bridge never receives, so a retained entry proves only a missing echo.

    Stubbing on that basis permanently contradicts an existing real result.
    The later echo then sees two answers, refuses, and can end the turn with
    more calls outstanding. This incident left 301 stubs across 74 threads.

    Therefore the Step declaration supplies the owed count and durable
    non-synthetic rows supply the held count. Only the shortfall is stubbed.
    Counting is required because one Step may legitimately repeat an identical
    call and arguments; there is no per-call identity on this side of the echo.
    """
    if not live.tool_step_ids:
        live.tools_settled.set()
        return
    cc_trust.distrust(live)
    owed = await run_in_db_unless_owned(db, partial(
        _unheld_result_counts,
        child_id, step_ids={step for step in live.tool_step_ids.values() if step},
        db=db,
    ))
    affected: list[str] = []
    for tool_id, step_id in list(live.tool_step_ids.items()):
        if owed.get(step_id, 0) <= 0:
            # This Step's results are already durable. Release ownership without
            # authoring a contradiction; a later echo binds the existing row.
            live.tool_names.pop(tool_id, None)
            live.tool_step_ids.pop(tool_id, None)
            continue
        owed[step_id] -= 1
        settled_step = await _settle_owner(
            live=live, child_id=child_id, identity=identity,
            tool_id=tool_id, step_id=step_id, bcast=bcast, db=db,
        )
        if settled_step not in affected:
            affected.append(settled_step)
    live.tools_settled.set()
    for step_id in affected:
        await emit_finalized_step(
            child_id=child_id, step_id=step_id, identity=identity,
            bcast=bcast, db=db,
        )


async def settle_committed_tool_result(
    *,
    live: _Live,
    child_id: str,
    identity: TurnIdentity,
    canonical,
    bcast: Broadcast,
) -> bool:
    """Release one exact CC tool debt once its canonical row is durable."""
    from core._tool_result_delivery_support import ToolResultBindingError
    from infra.database import get_db

    tool_id = canonical.provider_tool_call_id
    if not isinstance(tool_id, str) or not tool_id:
        raise ToolResultBindingError(
            "canonical CC tool result lacks provider call identity"
        )
    async with live.event_lock:
        if live.corsia_turn != identity:
            raise ToolResultBindingError(
                "canonical tool result no longer owns the active CORSIA Turn"
            )
        step_id = live.tool_step_ids.get(tool_id)
        if step_id != canonical.assistant_step_id:
            raise ToolResultBindingError(
                "canonical CC tool result changed Assistant Step ownership"
            )
        name = live.tool_names.pop(tool_id, canonical.name)
        live.tool_step_ids.pop(tool_id)
        all_settled = not live.tool_step_ids
        step_settled = step_id not in live.tool_step_ids.values()
        if all_settled:
            live.tools_settled.set()
    metadata = {
        "tool_call_id": tool_id,
        "assistant_step_id": step_id,
        "name": canonical.name,
    }
    if canonical.is_error:
        metadata["is_error"] = True
    await observe_tool_result(
        child_id,
        canonical.content,
        metadata,
        None,
        origin=RegistrationOrigin(child_id, identity.turn_id, "cc_observer"),
    )
    bcast(child_id, turn_frame(identity, tool_done_frame(
        child_id, tool_id, name,
        SettledResult.from_result(canonical), canonical.content[:8000],
    )))
    if step_settled:
        db = get_db()
        try:
            await emit_finalized_step(
                child_id=child_id,
                step_id=step_id,
                identity=identity,
                bcast=bcast,
                db=db,
            )
        finally:
            db.close()
    return all_settled



async def observe_tool_result(
    thread_id: str,
    content: str,
    metadata: dict,
    db,
    *,
    origin: RegistrationOrigin | None = None,
) -> None:
    """Observe one committed CC result without gating its primary delivery."""
    from registry.observer import observe_settled_tool_result

    await observe_settled_tool_result(
        thread_id, content, metadata, origin=origin, db=db,
    )


def tool_hint(name: str, args: dict) -> str:
    """Return a compact user-facing hint for one tool call."""
    args = args or {}
    if name == "Bash":
        hint = args.get("description") or args.get("command") or name
    elif name in ("Read", "Write", "Edit"):
        hint = args.get("file_path") or name
    elif name in ("Glob", "Grep"):
        hint = args.get("pattern") or name
    elif name == "Task":
        hint = args.get("description") or name
    else:
        hint = name
    return str(hint)[:80]
