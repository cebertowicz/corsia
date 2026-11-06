"""The name-to-tool lookup under ``ToolRegistry``, including reading another store's tools live."""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

from tools.base import Tool


class ToolStore:
    """Tools by name: the ones held here, then any a live store offers under another name."""

    def __init__(self) -> None:
        self._tools: dict[str, Tool] = {}
        self._live: Callable[[], ToolStore] | None = None
        self._exact = False

    def read_through(self, live: Callable[[], ToolStore]) -> None:
        """Offer the tools of the store ``live`` returns, called at every lookup and never copied.

        A set cut to exact tools stays exact: a native turn asks for this again on every turn.
        """
        if not self._exact:
            self._live = live

    def _visible(self) -> dict[str, Tool]:
        """Everything reachable by name; a tool held here keeps its name over a live one."""
        if self._live is None:
            return self._tools
        visible = dict(self._tools)
        # Copied first: a harness registry can be read off the event loop while an app connects.
        for name, tool in dict(self._live()._tools).items():
            visible.setdefault(name, tool)
        return visible

    def register(self, tool: Tool) -> None:
        """Register a tool."""
        self._tools[tool.name] = tool

    def unregister(self, name: str) -> None:
        """Unregister a tool by name."""
        self._tools.pop(name, None)

    def get(self, name: str) -> Tool | None:
        """Get a tool by name."""
        tool = self._tools.get(name)
        if tool is None and self._live is not None:
            return self._live().get(name)
        return tool

    def has(self, name: str) -> bool:
        """Check if a tool is registered."""
        return self.get(name) is not None

    def get_definitions(self) -> list[dict[str, Any]]:
        """Get all tool definitions in OpenAI format."""
        return [tool.to_schema() for tool in self._visible().values()]

    def retain_only(self, names: tuple[str, ...]) -> None:
        """Keep only an exact validated tool set for one disposable evaluation."""
        visible = self._visible()
        unknown = sorted(set(names) - set(visible))
        if unknown:
            raise ValueError("Unknown eval tool name(s): " + ", ".join(unknown))
        allowed = set(names)
        self._tools = {name: tool for name, tool in visible.items() if name in allowed}
        self._live = None
        self._exact = True

    def mcp_server_state(self, server: str) -> object | None:
        """The failure recorded for one connected-app server, looked up the way a tool is."""
        state = getattr(self, "mcp_server_states", {}).get(server)
        if state is None and self._live is not None:
            return self._live().mcp_server_state(server)
        return state

    @property
    def tool_names(self) -> list[str]:
        """Get list of registered tool names."""
        return list(self._visible())

    def __len__(self) -> int:
        return len(self._visible())

    def __contains__(self, name: str) -> bool:
        return self.has(name)
