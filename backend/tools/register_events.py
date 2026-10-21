"""Register tools in the events catalog domain."""
from __future__ import annotations

from tools.registry import ToolRegistry


def register(tools: ToolRegistry) -> None:
    """Install the read-only event rule inspection surface."""
    from tools.event_rules import EventRulesTool

    tools.register(EventRulesTool())
