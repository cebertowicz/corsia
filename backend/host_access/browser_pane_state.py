"""Shared-browser pane presence, screencast, and agent-presence state."""
from __future__ import annotations

import time
from typing import Callable

from host_access.browser_cdp import Rect
from host_access.browser_screencast import PaneScreencast

_panes: dict[str, set[str]] = {}
_capture_sizes: dict[str, tuple[int, int, bool]] = {}
_pane_reports: dict[str, dict[str, Callable[[dict], None]]] = {}
_pane_probes: dict[str, dict[str, Callable]] = {}
_screencasts: dict[str, dict[str, PaneScreencast]] = {}
_agent_seen: dict[str, float] = {}
_METRICS_COMMANDS = {("screenshot",), ("set", "viewport"), ("set", "device")}


def pane_connected(thread_id: str, connection_token: str, report: Callable[[dict], None] | None,
                   probe: Callable | None, *, on_probe: Callable[[str], None]) -> None:
    """Record one shared pane and resume any browser episode when it can be probed."""
    _panes.setdefault(thread_id, set()).add(connection_token)
    if report is not None:
        _pane_reports.setdefault(thread_id, {})[connection_token] = report
        if (seen := _agent_seen.get(thread_id)) is not None:
            report(_agent_message(time.monotonic() - seen))
    if probe is not None:
        _pane_probes.setdefault(thread_id, {})[connection_token] = probe
        on_probe(thread_id)


def pane_disconnected(thread_id: str, connection_token: str, *, on_last_probe: Callable[[str], None]) -> None:
    """Forget one pane connection without changing the presence of another pane."""
    reports = _pane_reports.get(thread_id)
    if reports is not None:
        reports.pop(connection_token, None)
        if not reports:
            del _pane_reports[thread_id]
    probes = _pane_probes.get(thread_id)
    if probes is not None:
        probes.pop(connection_token, None)
        if not probes:
            del _pane_probes[thread_id]
            on_last_probe(thread_id)
    connections = _panes.get(thread_id)
    if connections is None:
        return
    connections.discard(connection_token)
    if not connections:
        del _panes[thread_id]
        _capture_sizes.pop(thread_id, None)


def has_pane(thread_id: str) -> bool:
    return bool(_panes.get(thread_id))


def screencast_started(thread_id: str, connection_token: str, cast: PaneScreencast) -> None:
    _screencasts.setdefault(thread_id, {})[connection_token] = cast
    if (size := _capture_sizes.get(thread_id)) is not None:
        cast.capture_at(size[0], size[1], compact=size[2])


def capture_at(thread_id: str, width: int, height: int, *, compact: bool) -> None:
    _capture_sizes[thread_id] = (width, height, compact)
    for cast in screencasts(thread_id):
        cast.capture_at(width, height, compact=compact)


def screencast_ended(thread_id: str, connection_token: str, cast: PaneScreencast) -> None:
    casts = _screencasts.get(thread_id, {})
    if casts.get(connection_token) is cast:
        del casts[connection_token]
        if not casts:
            del _screencasts[thread_id]


def screencasts(thread_id: str) -> list[PaneScreencast]:
    return list(_screencasts.get(thread_id, {}).values())


def agent_acted(thread_id: str, field: Rect | None = None, label: Rect | None = None) -> None:
    _agent_seen[thread_id] = time.monotonic()
    message = _agent_message(0.0, field, label)
    for report in _pane_reports.get(thread_id, {}).values():
        report(message)


def page_command_starting(thread_id: str, args: tuple[str, ...]) -> None:
    if _changes_metrics(args):
        for cast in screencasts(thread_id):
            cast.hold()


async def page_command_finished(thread_id: str, args: tuple[str, ...]) -> None:
    if _changes_metrics(args):
        for cast in screencasts(thread_id):
            await cast.restart()


def _agent_message(age: float, field: Rect | None = None, label: Rect | None = None) -> dict:
    return {"type": "browser_agent", "age_ms": round(age * 1000), "field": field, "label": label}


def _changes_metrics(args: tuple[str, ...]) -> bool:
    return args[:1] in _METRICS_COMMANDS or args[:2] in _METRICS_COMMANDS
