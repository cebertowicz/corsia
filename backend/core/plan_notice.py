"""Attach the plan-upkeep notice to the model's view of a Turn's first work result.

Both live projection paths call this per result: native in
``core.turns_steps`` and the Claude Code/Codex bridge in
``subagents.harness_tool_bridge``. The notice appears only on the result that
answers the first tool call the Turn declared that is not one of Plans' own
(a call through the ``tool_call`` wrapper counts as the tool it names), so a
batch of parallel calls carries it once and every other result carries none.
A Turn that reads its plan first still gets the line on its first work call.
The words and facts come from ``plans.upkeep_notice``.

The notice sits after the projected result text: error and media detection
read the start, and the harness echo marker is appended after it. Nothing is
persisted. A rebuilt history therefore omits the notice, a bounded difference
of at most one line per Turn. A media result carries it after its placeholder
text. Reading the notice's facts is one more read on the projection path, and
it fails the projection the way the result's own rendering read would.
"""

from __future__ import annotations

import json
from typing import Any

from infra.database import get_db

#: Marks a first-work-call lookup the caller has not made yet.
UNLOOKED = object()


def plan_notice(
    *,
    thread_id: str | None,
    turn_id: str | None,
    tool_call_id: str | None,
    tool_name: str,
    first_work_call_id: Any = UNLOOKED,
) -> str | None:
    """Return the notice for this result when it answers the Turn's first work call, else None.

    ``tool_name`` is the dispatched tool. A caller that already read the Turn's
    first work call for this batch passes it, so the batch costs one read.
    """
    if not thread_id or not turn_id or not tool_call_id:
        return None
    from plans.public import is_plans_tool, upkeep_notice_for as notice_for

    if is_plans_tool(tool_name):
        return None
    if first_work_call_id is not UNLOOKED and first_work_call_id != tool_call_id:
        return None
    db = get_db()
    try:
        if first_work_call_id is UNLOOKED:
            first_work_call_id = first_work_call(db, thread_id, turn_id)
        if first_work_call_id != tool_call_id:
            return None
        return notice_for(db, thread_id, tool_name)
    finally:
        db.close()


class BatchPlanNotice:
    """Notices for one result batch, reading the Turn's first work call at most once."""

    def __init__(self, thread_id: str | None) -> None:
        self._thread_id = thread_id
        self._first_work_call_id: Any = UNLOOKED

    def for_result(
        self, *, turn_id: str | None, tool_call_id: str | None, tool_name: str,
    ) -> str | None:
        """Return the notice for one result of the batch, in the batch's order."""
        from plans.public import is_plans_tool

        if (
            self._first_work_call_id is UNLOOKED
            and self._thread_id and turn_id
            and not is_plans_tool(tool_name)
        ):
            db = get_db()
            try:
                self._first_work_call_id = first_work_call(db, self._thread_id, turn_id)
            finally:
                db.close()
        return plan_notice(
            thread_id=self._thread_id, turn_id=turn_id, tool_call_id=tool_call_id,
            tool_name=tool_name, first_work_call_id=self._first_work_call_id,
        )


def _is_plans_call(call: dict) -> bool:
    """Return whether a declared call dispatches to one of Plans' own tools."""
    from plans.public import is_plans_tool
    from tools.tool_surface import resolve_dispatch

    function = call.get("function")
    source = function if isinstance(function, dict) else call
    name = source.get("name")
    arguments = source.get("arguments")
    if isinstance(arguments, str):
        try:
            arguments = json.loads(arguments)
        except ValueError:
            arguments = None
    if not isinstance(name, str):
        return False
    dispatched_name, _arguments, _dispatcher = resolve_dispatch(name, arguments)
    return is_plans_tool(dispatched_name)


def first_work_call(db, thread_id: str, turn_id: str) -> str | None:
    """Return the id of the first declared tool call of the Turn that is not a Plans tool.

    The predicate is exactly ``idx_messages_turn_calls``' key and partial
    condition, so the read seeks the Turn's own rows. Left to itself the planner
    prefers ``idx_messages_session_seq`` to skip the sort and steps every
    assistant row of the thread, so the index is named. The first call is
    nearly always in the first row, so rows are read one at a time and the
    cursor closed on return: each row fetched is another release and retake of
    the interpreter lock while other threads run.
    """
    cursor = db.execute(
        """SELECT metadata FROM messages INDEXED BY idx_messages_turn_calls
           WHERE thread_id=? AND role='assistant' AND json_valid(metadata)
             AND json_extract(metadata, '$.turn_id')=?
           ORDER BY seq""",
        (thread_id, turn_id),
    )
    try:
        for row in cursor:
            metadata = json.loads(row["metadata"])
            calls = metadata.get("llm_tool_calls") or metadata.get("tool_calls") or []
            for call in calls:
                if isinstance(call, dict) and call.get("id") and not _is_plans_call(call):
                    return str(call["id"])
        return None
    finally:
        cursor.close()


def with_notice(projected: str, notice: str | None) -> str:
    """Return the projected result with the notice on its own line after it."""
    return projected if not notice else f"{projected}\n\n{notice}"
