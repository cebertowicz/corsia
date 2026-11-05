# Connected apps

A connected app is an MCP server that gives CORSIA agents tools for another service or program. CORSIA also connects accounts such as Google and Microsoft. This page explains how those connections reach agents and how outside events can wake them.

## Adding an app

In **Settings → Connected apps**, a person can add a local program or a remote server. CORSIA can also discover configurations to import from Claude Desktop, Claude Code, Cursor, Codex, OpenClaw and Hermes Agent (`backend/tools/mcp_import.py`). An agent can propose an app; the person approves it before it becomes usable.

A person chooses whether an app reaches all projects or selected projects, including whether it reaches work outside projects. A delegated worker cannot use an app its parent cannot reach (`backend/connections/app_reach.py`, `backend/tools/tool_surface.py`). See [Connected apps security](../security/connected-apps.md) for approval, keys and sandboxing.

## Tools on demand

CORSIA does not have to place every connected app's tool schema in every turn. The agent can load a server's tool index with `tools_load`, load the needed schema by name, then call it with `tool_call`. Availability and reach are checked for the calling agent (`backend/tools/tool_surface.py`).

## Connected accounts

Google connects Gmail, Calendar and, when granted, Drive and Docs. Microsoft connects mail, calendar and OneDrive. Other mail hosts can be added as mail accounts. Messages connects to Messages on this computer and must be enabled by the person. Each account has its own reach (`backend/connections/connected_accounts.py`, `backend/connections/messages/account.py`). See [Connected apps security](../security/connected-apps.md) for what these connections can access.

## Outside events

Webhooks deliver events from services such as GitHub to CORSIA. Event rules can route matching events to agents, waking them to work. GitHub's connector also produces a per-head event when a pull request's check suites settle; it is enabled outside test mode unless `CORSIA_GITHUB_HEAD_CHECKS_ENABLE=0` (`backend/events/connectors/github_head_checks.py`). A connected app does not by itself mean every outside event wakes an agent: delivery depends on the event and its routing rules.

## Where it lives

| Part | Path |
|---|---|
| App configuration and import | `backend/api/mcp.py`, `backend/tools/mcp_import.py` |
| MCP connections and tool discovery | `backend/tools/mcp_runtime.py`, `backend/tools/tool_surface.py` |
| Connected accounts | `backend/connections/` |
| Webhook intake and event routing | `backend/api/webhooks.py`, `backend/events/` |

Checked against the code at `7a8c55d84`.
