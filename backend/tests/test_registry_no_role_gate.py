"""There is no legacy ``session_role``-based tool gating.

``get_definitions()`` returns the full registered tool set regardless of session
context, and ``ToolRegistry`` exposes no role-gate surface. This test prevents
re-introducing ``get_definitions_for_role`` or ``_get_blocked_tools_for_role``.
"""

from __future__ import annotations

from typing import Any

from tools.base import Tool
from tools.registry import ToolRegistry


class _StubTool(Tool):
    def __init__(self, name: str) -> None:
        self._name = name

    @property
    def name(self) -> str:
        return self._name

    @property
    def description(self) -> str:
        return f"stub {self._name}"

    @property
    def parameters(self) -> dict[str, Any]:
        return {"type": "object", "properties": {}}

    async def execute(self, **kwargs: Any) -> str:
        return "ok"


def test_get_definitions_returns_full_set_session_independent():
    """Every registered tool is offered; nothing is filtered by a session role."""
    reg = ToolRegistry()
    reg.register(_StubTool("alpha"))
    reg.register(_StubTool("beta"))

    names = {d["function"]["name"] for d in reg.get_definitions()}
    assert names == {"alpha", "beta"}


def test_no_role_gate_surface_on_registry():
    """The legacy ``session_role``-keyed gate must not exist."""
    reg = ToolRegistry()
    assert not hasattr(reg, "get_definitions_for_role")

    import tools.registry as registry_mod
    assert not hasattr(registry_mod, "_get_blocked_tools_for_role")
