"""Browser-pane operations on the composed browser session owner."""
from __future__ import annotations

import tempfile
from dataclasses import dataclass
from pathlib import Path

from host_access.browser_streaming import StreamPortResult
from host_access.processes import PROCESS_OWNER

__all__ = ["HISTORY_ACTIONS", "PaneAppearance", "go", "match_pane", "open_url", "repaint", "stream_port"]

HISTORY_ACTIONS = frozenset({"back", "forward", "reload"})


@dataclass(frozen=True)
class PaneAppearance:
    """The pane's size in CSS pixels, its pixel density and CORSIA's resolved theme."""

    width: int
    height: int
    scale: float
    scheme: str

    @classmethod
    def parse(cls, data: dict) -> PaneAppearance | None:
        """Accept only a screen-sized pane and a light or dark theme."""
        numbers = [data.get(key) for key in ("width", "height", "scale")]
        if not all(isinstance(value, (int, float)) and not isinstance(value, bool) for value in numbers):
            return None
        width, height, scale = (float(value) for value in numbers if isinstance(value, (int, float)))
        if not (200 <= width <= 7680 and 150 <= height <= 4320 and 1 <= scale <= 4):
            return None
        if data.get("scheme") not in {"light", "dark"}:
            return None
        return cls(int(width), int(height), round(scale, 2), data["scheme"])


async def stream_port(thread_id: str) -> StreamPortResult:
    return await PROCESS_OWNER.browser_sessions().stream_port(thread_id)


async def open_url(thread_id: str, url: str, connection_token: str | None = None) -> str:
    """Open a pane URL: the holding stream navigates the open browser; without one, only a new session starts."""
    return await PROCESS_OWNER.browser_sessions().run(
        thread_id, ("open", url), control_token=connection_token,
        session="new" if connection_token is None else "existing",
    )


async def go(thread_id: str, action: str, connection_token: str) -> str:
    """Go back, forward or reload in the open browser, for the holding stream."""
    if action not in HISTORY_ACTIONS:
        raise ValueError(f"unknown browser action: {action}")
    return await PROCESS_OWNER.browser_sessions().run(
        thread_id, (action,), control_token=connection_token, session="existing",
    )


async def repaint(thread_id: str, connection_token: str) -> None:
    """Make Chrome draw the page afresh: after a size or theme change it may send no frame until the page changes."""
    with tempfile.TemporaryDirectory() as folder:
        await PROCESS_OWNER.browser_sessions().run(
            thread_id, ("screenshot", str(Path(folder) / "repaint.jpg")),
            control_token=connection_token, session="existing",
        )


async def match_pane(thread_id: str, appearance: PaneAppearance, connection_token: str) -> None:
    """Show the open browser's page at the pane's size, pixel density and CORSIA theme."""
    owner = PROCESS_OWNER.browser_sessions()
    for args in (("set", "viewport", str(appearance.width), str(appearance.height), f"{appearance.scale:g}"),
                 ("set", "media", appearance.scheme)):
        await owner.run(thread_id, args, control_token=connection_token, session="existing")
