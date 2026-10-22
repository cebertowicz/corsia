"""Claude Code tool-boundary admission: what the provider is allowed to call.

One question, asked in two places by the CC event pump: did this turn cross the
tool boundary CORSIA requires? A CC turn must reach tools only through the
CORSIA MCP server, so a native tool, a malformed tool call, a non-CORSIA MCP
server, or a provider-side skill/slash-command surface is a boundary violation
that gets recorded on the transcript rather than silently tolerated.

Two entry points, deliberately separate because they answer at different
moments with different evidence:

* :func:`_tool_boundary_violation_from_init` reads the provider's ``init``
  frame, so it can see the declared surface before any turn runs;
* :func:`_tool_bridge_unavailable_from_init` distinguishes the narrower case
  where the CORSIA server is present but still starting, which is not a
  violation and must not be recorded as one.

Split out of ``cc_runtime_events.py`` as part of the O5/O8 decomposition. This
group had ZERO references anywhere else in the backend, so the cut moves the
functions and their one recording writer together without changing any caller
outside the pump.
"""

from __future__ import annotations


def _is_context_limit_result(ev: dict) -> bool:
    if not ev.get("is_error"):
        return False
    from subagents.provider_context_limits import is_provider_context_limit

    return is_provider_context_limit({
        "result": ev.get("result"),
        "stop_reason": ev.get("stop_reason"),
        "subtype": ev.get("subtype"),
    })


def _mcp_server_name(server: object) -> str:
    if isinstance(server, dict):
        return str(server.get("name") or server.get("id") or "")
    return str(server)


_CORSIA_MCP_PREFIX = "mcp__corsia__"


def _is_corsia_mcp_tool(tool: object) -> bool:
    return str(tool).startswith(_CORSIA_MCP_PREFIX)


def _is_served_corsia_tool(tool: object, definitions: tuple[dict, ...] | None) -> bool:
    """Whether the live handle's served catalog holds this exact CORSIA name.

    Claude Code refuses a prefixed name the bridge never listed, so only a
    listed name can reach CORSIA. ``definitions=None`` means the caller cannot
    vouch for what was listed (the handle is no longer the thread's current
    one), so any prefixed name stays owed to the bridge: the fail-safe, since
    settling a name the bridge then runs would write two results for one call.
    """
    if not _is_corsia_mcp_tool(tool):
        return False
    if definitions is None:
        return True
    bare = str(tool)[len(_CORSIA_MCP_PREFIX):]
    return any(definition["function"]["name"] == bare for definition in definitions)


def admitted_native_tools(live: object) -> tuple[str, ...]:
    """Claude Code's own tools this launch may run; none unless it is confined.

    A ``review`` launch reads with Read, Grep and Glob and has no CORSIA tool
    bridge (event-rules design §8.1). Its admitted calls and their results
    stay in the provider's own transcript: CORSIA declares no tool call for
    them and binds no result. Any other tool on that launch crosses the
    boundary, since nothing could serve it.
    """
    from subagents.cc_invocation import CONFINEMENT_REVIEW, REVIEW_NATIVE_TOOLS

    session = getattr(live, "session", None)
    if getattr(session, "confinement", None) == CONFINEMENT_REVIEW:
        return REVIEW_NATIVE_TOOLS
    return ()


def unadmitted_tool_names(raw_uses: list, admitted: tuple[str, ...]) -> list[str]:
    """Names of a confined step's tool calls outside its admitted tools."""
    return sorted({
        str(tool.get("name") or "<unnamed>") if isinstance(tool, dict) else "<malformed>"
        for tool in raw_uses
        if not isinstance(tool, dict) or tool.get("name") not in admitted
    })


_MCP_SERVER_STARTUP_STATUSES = {
    "authenticating",
    "connecting",
    "handshaking",
    "init",
    "initializing",
    "loading",
    "pending",
    "starting",
}


def _mcp_server_is_starting(server: object) -> bool:
    if not isinstance(server, dict):
        return False
    return str(server.get("status") or "").lower() in _MCP_SERVER_STARTUP_STATUSES


def _tool_boundary_violation_payload(
    *,
    capability: str | None = None,
    native_tools: list[str] | None = None,
    non_corsia_mcp: list[str] | None = None,
    provider_skills: list[str] | None = None,
    provider_slash_commands: list[str] | None = None,
) -> tuple[dict, str]:
    meta = {
        "cc": True,
        "type": "tool_boundary_violation",
        "event_type": "harness.tool_boundary_violation",
        "harness": "claude-code",
        "repair_owner": "execution-dispatch",
        "capability": capability,
        "native_tools": native_tools or [],
        "non_corsia_mcp_servers": non_corsia_mcp or [],
        "provider_skills": provider_skills or [],
        "provider_slash_commands": provider_slash_commands or [],
    }
    content = (
        "Claude Code attempted a callable capability outside CORSIA. "
        "External harness tools must run through the CORSIA tool bridge."
    )
    return meta, content


def _tool_bridge_unavailable_from_init(ev: dict) -> tuple[dict, str] | None:
    tools = [str(tool) for tool in (ev.get("tools") or []) if str(tool)]
    if any(_is_corsia_mcp_tool(tool) for tool in tools):
        return None
    bridge_unavailable = any(
        _mcp_server_name(server) == "corsia" and not _mcp_server_is_starting(server)
        for server in (ev.get("mcp_servers") or [])
    )
    if not bridge_unavailable:
        return None
    meta = {
        "cc": True,
        "type": "tool_bridge_unavailable",
        "event_type": "harness.tool_bridge_unavailable",
        "harness": "claude-code",
        "repair_owner": "execution-dispatch",
        "missing_corsia_tools": True,
    }
    content = (
        "Claude Code reported the CORSIA MCP server as ready, but did not "
        "report CORSIA tool bridge tools."
    )
    return meta, content


def _tool_boundary_violation_from_init(
    ev: dict, admitted: tuple[str, ...] = (),
) -> tuple[dict, str] | None:
    tools = [
        str(tool) for tool in (ev.get("tools") or [])
        if str(tool) and str(tool) not in admitted
    ]
    mcp_server_names = [
        name for name in (_mcp_server_name(server) for server in (ev.get("mcp_servers") or []))
        if name
    ]
    native_tools = [
        tool for tool in tools if not _is_corsia_mcp_tool(tool)
    ]
    non_corsia_mcp = [
        name for name in mcp_server_names if name != "corsia"
    ]
    provider_skills = [str(skill) for skill in (ev.get("skills") or []) if str(skill)]
    provider_slash_commands = [
        str(command)
        for command in (ev.get("slash_commands") or [])
        if str(command)
    ]
    if (
        not native_tools
        and not non_corsia_mcp
        and not provider_skills
        and not provider_slash_commands
    ):
        return None
    return _tool_boundary_violation_payload(
        capability="init",
        native_tools=native_tools,
        non_corsia_mcp=non_corsia_mcp,
        provider_skills=provider_skills,
        provider_slash_commands=provider_slash_commands,
    )
