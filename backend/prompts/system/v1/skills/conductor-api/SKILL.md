---
name: conductor-api
description: Use when a designated conductor (workspace or project) needs to inspect or change CORSIA through corsia_api, create or promote an agent, or install its responsibility file, thread skill or supporting scripts. Covers route boundaries, retry safety and next-turn delivery. Not for human authentication or host-access authority.
corsia:
  handler:
    kind: instruction
---

# Conductor API and agent setup

Load `corsia_api` from the `conductor` pack. It calls the real API handlers in-process with your server-bound identity; no credential is needed. Every designated conductor, workspace or project, has full API access except the person-only list below. Other agents do not have the API tool. A project conductor's reach is not limited to its project: the API acts for the whole user, every project and the workspace's settings included. As a project conductor, change only your own project unless the person asked for more.

Use it for authorized setup, operations and inspection: projects, agents, model profiles, providers and models, network servers, accounts and bindings, MCP servers, key checks, documents and costs. Before a write, read the target and know its route schema. API access never grants authority to choose a new effect for the user. Prefer a purpose-built native tool when one already owns the operation.

Everything else is open. The person-only list: authentication, users, sessions, passwords, tokens, credentials and secrets; host-access approvals and leases; setting, clearing or removing a stored key, and deleting a provider or network server that holds one; answering the person's decisions; granting conductor designation; signing the person's subscription accounts in or out, or removing one; inbound webhooks; UI actions recorded as the person (their plan edits, seen state, comments and reviews); dismissing a security alert; and calling this tool recursively. Credential and host-access fields, a provider's `api_base`, protected file paths and linking a repository outside the workspace are refused even on an open route. A few handlers still require the signed-in person themselves (for example a full restart; `restart-self` is yours).

If refused, name the operation and direct the person to the CORSIA surface where they do it. Never try a token, shell HTTP call, database edit, encoded route or another identity to evade the denial.

Responses and requests are bounded. Ask for a smaller page or summary when the output is too large. A timeout or handler failure can follow a successful write: read the destination before retrying. An API error reports its status without exposing handler exception text.

## Bring over an MCP server

Call `GET /api/mcp/sources` to see only detected server names, kinds and source apps. With the user's approval, call `POST /api/mcp/import` using only the returned `{source, name}` pairs; it rereads the client configuration server-side, writes the canonical `tools.mcpServers` setting, reloads MCP sessions, and returns each server's working or failed status. Never put configuration, arguments, URLs, environment values, headers or a secret in `corsia_api` or chat. To add a server CORSIA has not detected, have the user configure it in a supported client's human settings surface first (including any secret), then detect and import it. There is no CORSIA Settings UI for MCP connections.

## Keys in Settings › Keys

Agents use these keys but never read them. `GET /api/keys` returns each entry's metadata: `name`, `label`, `used_by`, `is_set`, `set_at` and `source` (`keys` for Settings › Keys), never a value. `POST /api/keys` with `{name, label, description}` adds an entry with no value; a name is 1-40 lowercase letters, digits or hyphens, and a name in use is refused. An added entry does nothing until code that uses it ships. Both routes accept any signed-in caller, so any agent, not only the conductor, can list entries or add an inert one; neither can set or read a value. Only the person sets, replaces, clears or removes a key, in Settings › Keys with their password; never ask for a key or password in chat. A conductor may check one: `POST /api/keys/{name}/check` runs one search with the saved key and returns only `{outcome, reason}` (`works`, `key_refused`, `failed`, `not_set`). `provider_keys` lists Anthropic and OpenAI (and any legacy provider that has a key) with `is_set` only; the person enters those in Settings › Providers, and an API key in the environment is ignored. `server_keys` lists each registered External or oMLX server by display name with `is_set` only; agents cannot set a server's key either, because the person enters it in Settings › Providers. Web search's entry is `web-search`: to prove it works, check it, or read its metadata (`is_set`) and run one `web_search` for `CORSIA`. Its text starts `Web search: key refused.` or `Web search failed: <reason>.` when it doesn't work.

## Worked example: change a requested model profile

1. Read the profile: `corsia_api(method="GET", path="/api/models/codex/gpt-5.6-terra/profile")`.
2. After confirming the user-requested profile value and the current schema, write it: `corsia_api(method="PATCH", path="/api/models/codex/gpt-5.6-terra/profile", body={"context_window": 128000})`.
3. Read the same profile back and verify the requested value. A successful HTTP status alone does not prove the setup outcome.

## Create or promote an agent

1. Read `model_catalog`; use an exact ready selector and respect local concurrency and user model-control policy.
2. Create a visible thread with `delegate_create_thread`, or read the existing target with `agent_roster_entry`.
3. Use `agent_roster_update` with the target's fresh `expected_updated_at`, `agent_enabled=true` and the approved responsibility. A same-state retry changes nothing.
4. Load `conductor_instruction_file_create` from the `conductor` pack. With `kind="responsibility"`, `target_thread_id`, `expected_revision="absent"` and approved `content`, create and attach `RESPONSIBILITY.md`. Require `delivery.next_turn=true` before calling setup complete.
5. For a thread skill, use `kind="skill"`, a unique lowercase `name`, valid `SKILL.md` frontmatter and `content`. Optional `supporting_files` maps one-level `scripts/` or `references/` paths to text. Scripts are installed, never run by this tool. The receipt proves the skill appears in the target's next-turn skills list; a shadowed skill is an incomplete install.
6. Keep the returned bundle `revision` for later edits; `absent` protects creation. Retrying the same desired bytes is idempotent. For stale revisions, read current files before changing them. Verify roster, attached responsibility and skill delivery through the target's fresh turn.
