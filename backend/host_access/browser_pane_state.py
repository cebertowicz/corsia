"""Shared-browser pane presence, screencast, and agent-presence state."""
from __future__ import annotations

import time
from typing import Awaitable, Callable, Mapping

from host_access.browser_cdp import PasswordProbe, Rect
from host_access.browser_screencast import PaneScreencast

# Each guarded visit's known password nodes; the probe applies those of the visit the page is on.
KnownNodes = Mapping[str, frozenset[int]]
Probe = Callable[[KnownNodes], Awaitable[PasswordProbe]]

_panes: dict[str, set[str]] = {}
_capture_sizes: dict[str, tuple[int, int, bool]] = {}
_pane_reports: dict[str, dict[str, Callable[[dict], None]]] = {}
# Each connected pane's probe of the shared page, over the page endpoint it already holds.
_pane_probes: dict[str, dict[str, Probe]] = {}
_screencasts: dict[str, dict[str, PaneScreencast]] = {}
# When the agent last acted in each thread's page, so a pane that connects later shows it there.
_agent_seen: dict[str, float] = {}
# agent-browser commands that change the page's device metrics; a screenshot changes them while it captures.
_METRICS_COMMANDS = {("screenshot",), ("set", "viewport"), ("set", "device")}


def pane_connected(thread_id: str, connection_token: str, report: Callable[[dict], None] | None,
                   probe: Probe | None, *, on_probe: Callable[[str], None]) -> None:
    """Record a shared pane, its relay callback and its page probe without granting exclusive page control.

    ``on_probe`` runs once the thread's page can be probed from this pane.
    """
    _panes.setdefault(thread_id, set()).add(connection_token)
    if report is not None:
        _pane_reports.setdefault(thread_id, {})[connection_token] = report
        if (seen := _agent_seen.get(thread_id)) is not None:
            report(_agent_message(time.monotonic() - seen))
    if probe is not None:
        _pane_probes.setdefault(thread_id, {})[connection_token] = probe
        on_probe(thread_id)


def pane_disconnected(thread_id: str, connection_token: str, *, on_last_probe: Callable[[str], None]) -> None:
    """Forget one closed pane connection without changing another pane's presence.

    ``on_last_probe`` runs once no pane is left to probe the thread's page.
    """
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
    """Return whether a person can currently see this thread's shared browser."""
    return bool(_panes.get(thread_id))


def pane_probes(thread_id: str) -> list[Probe]:
    """Every connected pane's probe of this thread's one shared page."""
    return list(_pane_probes.get(thread_id, {}).values())


def report(thread_id: str, message: dict) -> None:
    """Relay one message to each of the thread's connected panes."""
    for relay in _pane_reports.get(thread_id, {}).values():
        relay(message)


def screencast_started(thread_id: str, connection_token: str, cast: PaneScreencast) -> None:
    _screencasts.setdefault(thread_id, {})[connection_token] = cast
    if (size := _capture_sizes.get(thread_id)) is not None:
        cast.capture_at(size[0], size[1], compact=size[2])


def capture_at(thread_id: str, width: int, height: int, *, compact: bool) -> None:
    """Size current and not-yet-connected screencasts from the pane's latest appearance."""
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
    """Every pane screencast showing this thread's one shared page."""
    return list(_screencasts.get(thread_id, {}).values())


def agent_acted(thread_id: str, field: Rect | None = None, label: Rect | None = None) -> None:
    """Show the thread's panes that its agent is in the page, and the field it wrote, if any."""
    _agent_seen[thread_id] = time.monotonic()
    report(thread_id, _agent_message(0.0, field, label))


def agent_left(thread_id: str) -> None:
    """Forget the agent's presence once the thread's browser has closed."""
    _agent_seen.pop(thread_id, None)


def _agent_message(age: float, field: Rect | None = None, label: Rect | None = None) -> dict:
    # An age rather than a time, so the pane needs no clock agreement with this server.
    return {"type": "browser_agent", "age_ms": round(age * 1000), "field": field, "label": label}


def page_command_starting(thread_id: str, args: tuple[str, ...]) -> None:
    """Hold the thread's pane pictures while a command changes the page's metrics."""
    if _changes_metrics(args):
        for cast in screencasts(thread_id):
            cast.hold()


async def page_command_finished(thread_id: str, args: tuple[str, ...]) -> None:
    """Restart the thread's pane screencasts after a command that changed the page's metrics."""
    if _changes_metrics(args):
        for cast in screencasts(thread_id):
            await cast.restart()


def _changes_metrics(args: tuple[str, ...]) -> bool:
    return args[:1] in _METRICS_COMMANDS or args[:2] in _METRICS_COMMANDS
