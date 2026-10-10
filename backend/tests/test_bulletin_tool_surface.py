from __future__ import annotations

from infra import state
from tools.bulletins import BulletinDispositionTool
from tools.registry import ToolRegistry
from tools.tool_surface import reachable_tool_names


def _reachable_for(thread_id: str | None, registry: ToolRegistry) -> list[str]:
    marker = state._current_thread_id.set(thread_id)
    try:
        return reachable_tool_names(registry)
    finally:
        state._current_thread_id.reset(marker)


def test_disposition_tool_is_discoverable_for_every_thread() -> None:
    registry = ToolRegistry()
    registry.register(BulletinDispositionTool())
    no_thread = _reachable_for(None, registry)
    ordinary = _reachable_for("ordinary", registry)
    conductor = _reachable_for("conductor", registry)
    assert no_thread == ordinary == conductor
    assert "bulletin_disposition" in no_thread
