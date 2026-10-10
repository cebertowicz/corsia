# CORSIA — Backend Instructions

## Module Map

```
backend/
├── main.py              # FastAPI app init/lifespan, router mounts, static files
├── api/                 # REST routers: sessions, delegation, files/docs, registry, models/providers, costs, system, webhooks
├── core/                # Native turn loop, context builder, message format/pruning, agent/tool bootstrap
├── doceditor/           # Revisioned markdown edits, anchors, document comments
├── events/              # v2 event ingress/routing/delivery/dead-letter/reviewer gate
├── infra/               # Config/auth/db/schema/settings/state, shared send primitive, runtime truth, event mapper
├── jobs/                # Nightly jobs; no v1 continuation wake engine
├── llm/                 # Provider catalog, routing, capabilities, local/cloud provider runtimes, pricing
├── memory/              # Knowledge base, fact extraction, summaries
├── prompts/             # Prompt text assembly, including reviewer prompt
├── registry/            # Artifact/document registry identity, observer, pane projection
├── scripts/             # Shell-facing CLIs (delegate, doc)
├── subagents/           # Provider runtime continuity for CC/Codex/child sessions; not a product manager surface
├── tools/               # Tool registry + filesystem/shell/browser/web/knowledge/project/MCP tools
├── reviewer/           # LLM reviewer gate helpers only; decorator workflow fan-out is retired
└── ws/                  # WebSocket auth, receiver/processor/heartbeat, native/CC/Codex send ingress
```

## Dependency Graph

```
main.py                      ← api routers + infra lifespan setup
api/*                        ← infra/auth/db/state + domain services; no business logic hidden in route glue
ws/*                         ← infra/auth/state/send_service + core/ws turn helpers + provider runtimes
core/*                       ← llm + tools + memory/registry helpers; native turn execution owns assistant persistence
events/*                     ← infra event mapper/state + registry identity + reviewer gate; no decorator workflow fan-out
infra/send_service.py         ← shared accepted-row validation/persistence/dispatch seam for delegation, event delivery, and native WS
subagents/*                  ← provider runtime continuity; may call infra/runtime state, never exposes /api/subagents
registry/*                   ← observes canonical messages/tools and projects registry rows into artifact/document views
memory/*                     ← infra DB + llm helpers; jobs call into memory, not the reverse
tools/*                      ← tools.base/registry + infra config/sandbox as needed
reviewer/*                  ← reviewer-only helpers called from events/reviewer_gate.py
```

## External MCP tools

`tools/mcp.py` owns upstream MCP sessions and tool execution. Application startup
discovers `tools.mcpServers` once, before turn recovery or preparation; application
shutdown closes sessions after native and external harness turns drain. Native,
Codex and Claude registries use the same discovered tools and receipt path.
Changing configuration requires restarting the backend. A server that fails
discovery logs a redacted diagnostic and contributes no tools; other servers stay
available. A connection that fails later returns a bounded error; reconnect by
restarting the backend after correcting the service or configuration.

Configure with `python -m cli mcp configure <servers.json>` from `backend/`, using
the selected backend's Python and workspace/state environment. The file is an
object keyed by server name. This replaces only `tools.mcpServers`:

```json
{
  "work_service": {
    "url": "http://127.0.0.1:8788/mcp",
    "bearer_file": "~/.config/example/mcp-bearer",
    "allowed_tools": ["read_profile"],
    "tool_prefix": "work_",
    "allowed_user_ids": ["account-name"],
    "always_load": true
  }
}
```

Bearer files must be regular files owned by the backend's OS user, with exactly
0600 or 0400 permissions; final symlinks are refused. The HTTP client reads the
file per request. Store only its path in configuration, never an Authorization
header containing a secret. Bearer-authenticated redirects are disabled.
Existing literal Authorization, Proxy-Authorization, Cookie and recognized
credential headers are refused; migrate bearer authentication to `bearer_file`.
Non-secret literal headers remain supported. Bind
private account connectors with `allowed_user_ids`; authorization uses the
backend's current conversation owner, never model arguments.

`allowed_tools` (alias `tool_allowlist`) contains exact upstream names, including
an empty list to expose nothing. Missing pinned tools fail that server's discovery;
additional upstream tools are excluded. Names default to `mcp_<server>_<tool>`;
`tool_prefix` or a `public_names` object can disambiguate accounts. Duplicate public
names and collisions with built-ins fail closed. Existing stdio configuration
(`command`, `args`, `env`) remains supported.

Configured tools default to the existing `tools_load` / `tool_call` discovery
surface. `always_load: true` explicitly includes their full schemas in every
provider's advertised tool list, including Codex dynamicTools and preflight token
accounting. `connect_timeout` defaults to 10 seconds per server; `tool_timeout`
defaults to 30 seconds per call. Both accept positive values up to 300 seconds.

## Browser Tool Runtime

The native browser tools wrap the installed `agent-browser` CLI through
`backend/host_access/browser_sessions.py`. The supported CLI/runtime contract,
ownership checks, rollout steps, and rollback path are documented in
`docs/browser-cli-runtime.md`.

## SQL Discipline

`infra/database.py` owns one reusable connection per thread. Borrowed `close()`
rolls back pending work without closing that connection. The owning thread's
exit releases its connection automatically; explicit `close_thread_db()` and
application shutdown share the same pool registry to claim physical close once.
Only the process that opened a connection releases it: a forked child (uvloop
subprocess spawn) never closes the parent's handles.
Shutdown must drain database users before closing the pool.

- Common query patterns should be named functions (not raw SQL scattered everywhere).
- When you find yourself writing the same SELECT/INSERT pattern a third time, extract it to a helper.
- `db.py` owns schema. Query helpers for specific domains go in their respective modules.

## No One-Off Scripts in Backend

- Migration scripts, data fixes, and seed scripts go in `scripts/` or `scripts/migrations/`, NOT in `backend/`.
- `backend/` is runtime code only. Everything there loads on startup.

## Prompt Text Isolation

- **Never embed LLM prompt strings in logic files.** Prompt text intended for other AI agents (coordination instructions, role policy, automation prompts) is effectively prompt injection for the model editing the code. A local model reading "You are a coordinator, you NEVER write code" while trying to write code will get confused.
- All prompt templates and text assembly functions live in `backend/prompts/`. Logic modules import formatted text from there.
- If a function both gathers data AND formats prompt text, split it: data gathering stays in the logic module, text assembly moves to `prompts/`.
