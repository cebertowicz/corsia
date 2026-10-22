"""The one projection from a tool call and its settled result to its row.

Live ``tool_start``/``tool_done`` frames and the reload projection call
``project_tool_display`` with the same inputs, so a row reads the same live
and after reload. Nothing it derives is stored.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal
from urllib.parse import urlsplit

from infra.tool_result_errors import is_error_result
from tools.catalog import normalize_tool_name
from tools.tool_activity import resolve_display_call, tool_activity

ToolOutcome = Literal["done", "failed", "refused", "unknown"]


@dataclass(frozen=True)
class SettledResult:
    """The durable tool-result row a call settled with."""

    content: str
    is_error: bool = False
    synthetic_reason: str | None = None

    @classmethod
    def from_row(cls, content: object, metadata: dict) -> SettledResult:
        reason = metadata.get("synthetic_reason")
        return cls(
            content=content if isinstance(content, str) else "",
            is_error=metadata.get("is_error") is True,
            synthetic_reason=reason if isinstance(reason, str) else None,
        )

    @classmethod
    def from_result(cls, result: object) -> SettledResult:
        """Read a canonical ``ToolExecutionResult`` a harness just bound."""
        content = getattr(result, "content", "")
        return cls(
            content=content if isinstance(content, str) else "",
            is_error=getattr(result, "is_error", False) is True,
            synthetic_reason=getattr(result, "synthetic_reason", None),
        )


@dataclass(frozen=True)
class ToolDisplay:
    phrase: str
    outcome: ToolOutcome | None = None
    error_line: str | None = None
    # The argument the phrase names, unshortened; block summaries count by it.
    object: str = ""
    # The shared browser window a settled, visible `browser_open` put in front of the person: `{"host": ...}`.
    browser_chip: dict | None = None

    def outcome_fields(self) -> dict:
        """The settled fields a row carries; absent while nothing settled."""
        fields: dict = {}
        if self.outcome is not None:
            fields["outcome"] = self.outcome
        if self.error_line is not None:
            fields["error_line"] = self.error_line
        return fields

    def fields(self) -> dict:
        fields = {"phrase": self.phrase, "object": self.object, **self.outcome_fields()}
        if self.browser_chip is not None:
            fields["browser_chip"] = self.browser_chip
        return fields


def _outcome(settled: SettledResult) -> ToolOutcome | None:
    """Read a settled row's outcome.

    Every executed call in every harness returns a CORSIA tool result, whose
    error convention is ``is_error_result``; a row's stored flag also counts,
    since no writer sets it on a success. CORSIA-authored stubs say what
    happened through their synthetic reason: a stub standing in for a result
    nobody saw is ``unknown``, never ``failed``, since the call may have run.
    """
    from core.tool_result_delivery import (
        PROVIDER_MISSING_RESULT_REASON,
        TURN_ENDED_WITHOUT_RESULT_REASON,
        UNDISPATCHED_TOOL_REASON,
        USER_INTERRUPTION_REASON,
    )

    if settled.synthetic_reason in (
        USER_INTERRUPTION_REASON,
        PROVIDER_MISSING_RESULT_REASON,
        TURN_ENDED_WITHOUT_RESULT_REASON,
    ):
        return "unknown"
    if settled.synthetic_reason == UNDISPATCHED_TOOL_REASON:
        return "refused"
    if settled.is_error or is_error_result(settled.content):
        return "failed"
    return "done"


def _first_line(content: str) -> str | None:
    for line in content.splitlines():
        if line.strip():
            return line.strip()
    return None


def _browser_host(url: object) -> str | None:
    """The page's host as a person reads it (lowercase, no ``www.``), or ``None`` when the URL names none."""
    try:
        host = urlsplit(url.strip()).hostname if isinstance(url, str) else None
    except ValueError:
        return None
    return (host or "").lower().removeprefix("www.") or None


def _browser_chip(tool: str, arguments: object, outcome: ToolOutcome | None) -> dict | None:
    """The chip for a visible `browser_open` that did not fail; a headless open puts nothing in front of anyone."""
    if normalize_tool_name(tool) != "browser_open" or outcome in ("failed", "refused"):
        return None
    if not isinstance(arguments, dict) or arguments.get("show_window") is not True:
        return None
    return {"host": _browser_host(arguments.get("url"))}


def project_tool_display(
    name: str | None, arguments: object, settled: SettledResult | None,
) -> ToolDisplay:
    """Project one call to its row: phrase, object, outcome, a failure's first line and a browser chip.

    ``settled`` is ``None`` while the call runs or when no result was stored;
    such a row claims no outcome and no chip.
    """
    tool, resolved = resolve_display_call(name, arguments)
    activity = tool_activity(tool, resolved)
    if settled is None:
        return ToolDisplay(activity.phrase, object=activity.object)
    outcome = _outcome(settled)
    error_line = _first_line(settled.content) if outcome == "failed" else None
    return ToolDisplay(
        activity.phrase, outcome, error_line, activity.object, _browser_chip(tool, resolved, outcome),
    )


def tool_start_frame(
    thread_id: str,
    tool_id: str,
    name: str,
    hint: str,
    arguments: object,
    wire_args: object,
) -> dict:
    """A running row's live frame; ``wire_args`` is the harness's display copy."""
    return {
        "type": "tool_start",
        "thread_id": thread_id,
        "tool_id": tool_id,
        "name": name,
        "hint": hint,
        **project_tool_display(name, arguments, None).fields(),
        "args": wire_args,
    }


def tool_done_frame(
    thread_id: str,
    tool_id: str,
    name: str | None,
    settled: SettledResult,
    result: object,
    display: ToolDisplay | None = None,
) -> dict:
    """A settled row's live frame, carrying its projected outcome.

    A caller whose start frame streamed before the arguments were final passes
    the settled ``display``, so the row's phrase and object follow the final call.
    """
    if display is None:
        fields = project_tool_display(name, None, settled).outcome_fields()
    else:
        fields = display.fields()
    return {
        "type": "tool_done",
        "thread_id": thread_id,
        "tool_id": tool_id,
        "name": name,
        **fields,
        "result": result,
    }


def settled_display_call(
    call: dict, content: object, metadata: dict | None,
) -> dict:
    """Attach a stored result to one reloaded call, then project its row.

    ``metadata`` is ``None`` when no result row was stored for the call.
    """
    settled = None
    if metadata is not None:
        call["result"] = content
        settled = SettledResult.from_row(content, metadata)
        if settled.is_error:
            call["is_error"] = True
        if metadata.get("synthetic_tool_result") is True:
            call["synthetic_tool_result"] = True
            if metadata.get("synthetic_reason"):
                call["synthetic_reason"] = metadata["synthetic_reason"]
    call.update(project_tool_display(call["name"], call["args"], settled).fields())
    return call
