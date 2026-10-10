"""Agent-facing tools over the host-access process owner.

These are tool-layer surfaces, not process infrastructure. They previously lived
inside ``host_access/processes.py``, which forced that module to import
``tools.base`` — infrastructure depending on the tool layer above it. Keeping
them here leaves ``host_access`` free of any tool import.
"""

from __future__ import annotations

import asyncio
import json
from typing import Any, Mapping

# Imported as a MODULE, not by name: these tools are exercised by patching
# ``host_access.processes`` attributes, and binding the names here at import
# time would freeze the originals past any patch.
from automation import managed_process_recovery
from automation.managed_process_events import row_for
from host_access import processes
from infra.db_async import run_in_db
from infra.managed_process_store import LIST_LIMIT, get_handle, list_thread_handles
from tools.base import Tool

_STATUS_FIELDS = (
    "state",
    "description",
    "long_running",
    "outcome",
    "reason",
    "exit_code",
    "started_at",
    "last_output_at",
    "stalled_since",
    "ended_at",
    "log_truncated",
)
_UNSETTLED_STATES = {"running", "death_unverified"}


def _json_result(payload: Mapping[str, Any]) -> str:
    return json.dumps(
        payload,
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
    )


def _tool_thread_id(kwargs: dict[str, Any]) -> tuple[str, str | None]:
    thread_id = kwargs.pop("_thread_id", None)
    project_id = kwargs.pop("_thread_project_id", None)
    kwargs.pop("_thread_turn_id", None)
    if not isinstance(thread_id, str) or not thread_id:
        raise PermissionError("managed process tool requires exact thread identity")
    return thread_id, project_id


def _status_view(row: Mapping[str, Any]) -> dict[str, Any]:
    view = {
        "handle": row["handle"],
        **{field: row.get(field) for field in _STATUS_FIELDS},
    }
    view["long_running"] = bool(view["long_running"])
    view["log_truncated"] = bool(view["log_truncated"])
    return view


async def _owned_row(
    owner: processes.ProcessOwner,
    handle: str,
    thread_id: str,
) -> dict[str, Any]:
    try:
        return row_for(owner.status(handle, thread_id=thread_id))
    except KeyError:
        row = await run_in_db(get_handle, handle)
    if row is None:
        raise KeyError("managed process handle is unknown")
    if row["thread_id"] != thread_id:
        raise PermissionError("managed process handle is owned by another thread")
    return row


class ManagedProcessStatusTool(Tool):
    """Read one owned managed-process terminal state."""

    name = "managed_process_status"
    description = "Read the state of an opaque managed background-process handle."
    parameters = {
        "type": "object",
        "properties": {
            "handle": {
                "type": "string",
                "minLength": 1,
                "maxLength": 128,
            }
        },
        "required": ["handle"],
        "additionalProperties": False,
    }

    def __init__(self, owner: processes.ProcessOwner | None = None) -> None:
        self._owner = owner or processes.PROCESS_OWNER

    async def execute(self, handle: str, **kwargs: Any) -> str:
        thread_id, _project_id = _tool_thread_id(kwargs)
        row = await _owned_row(self._owner, handle, thread_id)
        return _json_result(_status_view(row))


class ManagedProcessLogsTool(Tool):
    """Read a bounded tail of one owned managed-process log."""

    name = "managed_process_logs"
    description = "Read bounded logs for an opaque managed background-process handle."
    parameters = {
        "type": "object",
        "properties": {
            "handle": {
                "type": "string",
                "minLength": 1,
                "maxLength": 128,
            },
            "limit_bytes": {
                "type": "integer",
                "minimum": 1,
                "maximum": 50_000,
            },
        },
        "required": ["handle"],
        "additionalProperties": False,
    }

    def __init__(self, owner: processes.ProcessOwner | None = None) -> None:
        self._owner = owner or processes.PROCESS_OWNER

    async def execute(
        self,
        handle: str,
        limit_bytes: int = 20_000,
        **kwargs: Any,
    ) -> str:
        thread_id, _project_id = _tool_thread_id(kwargs)
        row = await _owned_row(self._owner, handle, thread_id)
        try:
            logs = self._owner.read_logs(
                handle,
                thread_id=thread_id,
                limit_bytes=limit_bytes,
            )
        except KeyError:
            logs = managed_process_recovery.read_persisted_logs(
                self._owner, row, limit_bytes,
            )
        return _json_result({**_status_view(row), "logs": logs})


class ManagedProcessListTool(Tool):
    """List this thread's background handles, including pre-restart ones."""

    name = "managed_process_list"
    description = (
        "List this thread's managed background-process handles, unsettled first "
        f"then newest terminal history (max {LIST_LIMIT}), with their state."
    )
    parameters = {
        "type": "object",
        "properties": {},
        "additionalProperties": False,
    }

    def __init__(self, owner: processes.ProcessOwner | None = None) -> None:
        self._owner = owner or processes.PROCESS_OWNER

    async def execute(self, **kwargs: Any) -> str:
        thread_id, _project_id = _tool_thread_id(kwargs)
        rows = {
            row["handle"]: row
            for row in await run_in_db(list_thread_handles, thread_id)
        }
        for record in self._owner.records_for_thread(thread_id):
            rows[record.handle] = row_for(record)
        newest = sorted(
            rows.values(),
            key=lambda row: (row["state"] in _UNSETTLED_STATES, row["started_at"] or "", row["handle"]),
            reverse=True,
        )[:LIST_LIMIT]
        return _json_result({"handles": [_status_view(row) for row in newest]})


class ManagedProcessStopTool(Tool):
    """Stop exactly one owned managed-process handle without a live lease."""

    name = "managed_process_stop"
    description = "Stop an opaque managed background-process handle owned by this thread."
    parameters = ManagedProcessStatusTool.parameters

    def __init__(self, owner: processes.ProcessOwner | None = None) -> None:
        self._owner = owner or processes.PROCESS_OWNER

    async def execute(self, handle: str, **kwargs: Any) -> str:
        thread_id, _project_id = _tool_thread_id(kwargs)
        try:
            record = await self._owner.stop(
                handle,
                thread_id=thread_id,
                reason="managed_stop",
            )
        except processes.ProcessCleanupError as exc:
            managed_process_recovery.raise_live_stop_error(
                self._owner, handle, thread_id, exc,
            )
        except KeyError:
            row = await run_in_db(
                managed_process_recovery.stop_persisted_managed_process,
                handle,
                thread_id,
            )
            state = str(row["state"])
        else:
            state = record.terminal_state
        return _json_result({"handle": handle, "state": state})


class ProcessDiscoverTool(Tool):
    """Reviewer-gated bounded host process discovery outside Seatbelt."""

    name = "process_discover"
    description = (
        "Discover bounded host process metadata. Environments are never read; "
        "sensitive arguments are redacted."
    )
    parameters = {
        "type": "object",
        "properties": {
            "pid": {
                "type": ["integer", "null"],
                "minimum": 2,
                "maximum": 2_147_483_647,
            },
            "query": {
                "type": ["string", "null"],
                "maxLength": 200,
            },
            "limit": {
                "type": "integer",
                "minimum": 1,
                "maximum": 50,
            },
        },
        "required": [],
        "additionalProperties": False,
    }

    async def execute(
        self,
        pid: int | None = None,
        query: str | None = None,
        limit: int = 20,
        **kwargs: Any,
    ) -> str:
        thread_id, project_id = _tool_thread_id(kwargs)
        params = {"pid": pid, "query": query, "limit": limit}
        from tools.host_access_gate import consume_current_action_permit

        consumed = await consume_current_action_permit(
            thread_id=thread_id,
            project_id=project_id,
            tool=self,
            tool_name=self.name,
            params=params,
        )
        if consumed is None:
            raise PermissionError("process discovery requires a fresh host permit")
        discovered = await asyncio.to_thread(
            processes.discover_processes,
            pid=pid,
            query=query,
            limit=limit,
        )
        return _json_result(
            {
                "processes": [
                    {
                        "pid": process.pid,
                        "parent_pid": process.parent_pid,
                        "name": process.name,
                        "executable": process.executable,
                        "arguments": list(process.arguments),
                        "create_time": process.create_time,
                        "executable_fingerprint": (
                            process.executable_fingerprint
                        ),
                    }
                    for process in discovered
                ]
            }
        )


class ProcessSignalTool(Tool):
    """Reviewer-gated exact-PID signal with creation/executable binding."""

    name = "process_signal"
    description = (
        "Signal one exact PID previously returned by process_discover after "
        "revalidating its creation time and executable fingerprint."
    )
    parameters = {
        "type": "object",
        "properties": {
            "pid": {
                "type": "integer",
                "minimum": 2,
                "maximum": 2_147_483_647,
            },
            "create_time": {
                "type": "number",
                "minimum": 0,
            },
            "executable_fingerprint": {
                "type": "string",
                "minLength": 64,
                "maxLength": 64,
            },
            "signal": {
                "type": "string",
                "enum": ["interrupt", "terminate", "kill"],
            },
        },
        "required": [
            "pid",
            "create_time",
            "executable_fingerprint",
            "signal",
        ],
        "additionalProperties": False,
    }

    async def execute(
        self,
        pid: int,
        create_time: float,
        executable_fingerprint: str,
        signal: str,
        **kwargs: Any,
    ) -> str:
        thread_id, project_id = _tool_thread_id(kwargs)
        params = {
            "pid": pid,
            "create_time": create_time,
            "executable_fingerprint": executable_fingerprint,
            "signal": signal,
        }
        from tools.host_access_gate import consume_current_action_permit

        consumed = await consume_current_action_permit(
            thread_id=thread_id,
            project_id=project_id,
            tool=self,
            tool_name=self.name,
            params=params,
        )
        if consumed is None:
            raise PermissionError("process signaling requires a fresh host permit")
        work = asyncio.create_task(
            asyncio.to_thread(
                processes.signal_bound_process,
                pid=pid,
                create_time=create_time,
                executable_fingerprint=executable_fingerprint,
                signal_name=signal,
            ),
            name=f"host-process-signal:{pid}",
        )
        try:
            await asyncio.shield(work)
        except asyncio.CancelledError as cancellation:
            while not work.done():
                try:
                    await asyncio.shield(work)
                except asyncio.CancelledError:
                    continue
                except BaseException:
                    break
            try:
                work.result()
            except BaseException as exc:
                cancellation.add_note(
                    "host process signal settled with "
                    f"{type(exc).__name__}: {exc}"
                )
            raise cancellation
        return f"Signaled exact PID {pid} with {signal}."
