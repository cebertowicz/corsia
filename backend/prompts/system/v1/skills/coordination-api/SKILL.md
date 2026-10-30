---
name: coordination-api
description: Use when the CORSIA agent or a project conductor needs to inspect or change CORSIA through corsia_api, create or promote an agent, install its responsibility file, thread skill or supporting scripts, or add, fix or remove connected apps (tool servers, MCP servers, connected tools). Covers route boundaries, retry safety and next-turn delivery. Not for human authentication or host-access authority.
---

# Coordination API and agent setup

Load `corsia_api` from the `coordination` pack. It calls the real API handlers in-process with your server-bound identity; no credential is needed. The CORSIA agent and every project conductor have full API access except the person-only list below. Other agents do not have the API tool. A project conductor's reach is not limited to its project: the API acts for the whole user, every project and the workspace's settings included, except connected apps (below). As a project conductor, change only your own project unless the person asked for more.

Use it for authorized setup, operations and inspection: projects, agents, model profiles, providers and models, network servers, accounts and bindings, connected apps, key checks, documents and costs. Before a write, read the target and know its route schema. API access never grants authority to choose a new effect for the user. Prefer a purpose-built native tool when one already owns the operation.

Everything else is open. The person-only list: authentication, users, sessions, passwords, tokens, credentials and secrets; host-access approvals and leases; setting, clearing or removing a stored key, and deleting a provider or network server that holds one; answering the person's decisions; granting conductor designation; signing the person's subscription accounts in or out, or removing one; inbound webhooks; UI actions recorded as the person (their plan edits, seen state, comments and reviews); dismissing a security alert; and calling this tool recursively. Credential and host-access fields, a provider's `api_base`, protected file paths and linking a repository outside the workspace are refused even on an open route. A few handlers still require the signed-in person themselves (for example a full restart; `restart-self` is yours).

If refused, name the operation and direct the person to the CORSIA surface where they do it. Never try a token, shell HTTP call, database edit, encoded route or another identity to evade the denial.

Responses and requests are bounded. Ask for a smaller page or summary when the output is too large. A timeout or handler failure can follow a successful write: read the destination before retrying. An API error reports its status without exposing handler exception text.

## Connected apps (MCP servers)

To connect a tool for the person's work, start with skill `connect-tools`.

**Talk to the person in their words.** Call these "connected apps" and name the app ("GitHub"). Say "key", never token or a variable name; "Connected apps", never "tool servers" or "MCP" unless the person said it first.

Look first: `GET /api/mcp/servers`. Each app has a `state` and a `next_step` written for the person; do what it says and relay its words rather than diagnosing.

- You see the apps your project can use. Anything you add is for your project only (outside a project, for every project). Nothing you do reaches another project's apps.
- Before adding an app, look at the list. If an app with `on_here: false` already does the job, tell the person it's turned off here, using its `next_step`. If the app would help every project (for example GitHub or a calendar), don't add it for your project: ask CORSIA, the workspace agent, to connect it for every project, tell the person you asked, and stop. If you can't message CORSIA, ask the person to ask CORSIA.
- As CORSIA, when an agent asks you to connect an app for every project, add it yourself with `asked_by_thread_id` set to that agent's thread id (never `reach`); it is proposed for every project. Tell the person which agent and project asked, and that it waits for their approval.
- Add one: `POST /api/mcp/servers` with `{"name": "GitHub", "config": {"command": "npx", "args": ["-y", "<package>"]}, "key_refs": [{"kind": "env", "name": "GITHUB_TOKEN"}]}` (`"url"` instead of `"command"` for a hosted app; `key_refs` names the keys it needs, never their values). Change one: `PATCH /api/mcp/servers/{id}`. It is saved as `awaiting_approval` and nothing runs until the person approves it. Tell them what it will run, and where to approve it in the words of the new row's `next_step`.
- Bring one over from another app, only with the person's yes: `GET /api/mcp/sources` lists what other apps have, by `source` and `name`; `POST /api/mcp/import` with `{"servers": [{"source": "...", "name": "..."}]}` and only those pairs. Each comes back `awaiting_approval` like an added app, or with a `status` that says why not; relay it.
- Approving, declining and choosing which projects an app works in are the person's alone; you will be refused.
- `on_here: false`: the app is turned off for this project. You can't turn it on; relay its `next_step`.
- `failed` or `disconnected`: `POST /api/mcp/servers/{id}/reconnect` once. If it says to wait, wait 10 seconds. If it fails again, relay the `next_step` and stop.
- `needs_token` or `token_rejected`: you can't set keys. Relay the `next_step`. Never ask for a key in chat.
- `expired`: the request waited too long. Ask the person whether they still want it; if so, add it again.
- `needs_sign_in`: you can't sign in for the person. Relay the `next_step`.
- Remove: `DELETE /api/mcp/servers/{id}`, only when the person asked.
- Once an app is `working`, load its tools with `tools_load(server="<name>")`.

## Keys in Settings › Keys

Agents use these keys but never read them. `GET /api/keys` returns each entry's metadata: `name`, `label`, `used_by`, `is_set`, `set_at` and `source` (`keys` for Settings › Keys), never a value. `POST /api/keys` with `{name, label, description}` adds an entry with no value; a name is 1-40 lowercase letters, digits or hyphens, and a name in use is refused. An added entry does nothing until code that uses it ships. Both routes accept any signed-in caller, so any agent, not only the conductor, can list entries or add an inert one; neither can set or read a value. Only the person sets, replaces, clears or removes a key, in Settings › Keys with their password; never ask for a key or password in chat. A conductor may check one: `POST /api/keys/{name}/check` runs one search with the saved key and returns only `{outcome, reason}` (`works`, `key_refused`, `failed`, `not_set`). `provider_keys` lists Anthropic and OpenAI (and any legacy provider that has a key) with `is_set` only; the person enters those in Settings › Providers, and an API key in the environment is ignored. `server_keys` lists each registered External or oMLX server by display name with `is_set` only; agents cannot set a server's key either, because the person enters it in Settings › Providers. Web search's entry is `web-search`: to prove it works, check it, or read its metadata (`is_set`) and run one `web_search` for `CORSIA`. Its text starts `Web search: key refused.` or `Web search failed: <reason>.` when it doesn't work.

## Worked example: change a requested model profile

1. Read the profile: `corsia_api(method="GET", path="/api/models/codex/gpt-5.6-terra/profile")`.
2. After confirming the user-requested profile value and the current schema, write it: `corsia_api(method="PATCH", path="/api/models/codex/gpt-5.6-terra/profile", body={"context_window": 128000})`.
3. Read the same profile back and verify the requested value. A successful HTTP status alone does not prove the setup outcome.

## Create or promote an agent

1. Read `model_catalog`; use an exact ready selector and respect local concurrency and user model-control policy.
2. Create a visible thread with `delegate_create_thread`, or read the existing target with `agent_roster_entry`.
3. Use `agent_roster_update` with the target's fresh `expected_updated_at`, `agent_enabled=true` and the approved responsibility (and `name` to rename it). A same-state retry changes nothing.
4. Load `coordination_instruction_file_create` from the `coordination` pack. With `kind="responsibility"`, `target_thread_id`, `expected_revision="absent"` and approved `content`, create and attach `RESPONSIBILITY.md`. Require `delivery.next_turn=true` before calling setup complete.
5. Give the agent its home before its first turn. If its domain is an existing category in `shared/INDEX.md`, reuse it and leave that index as it is. Otherwise write `shared/<domain>/INDEX.md` with `write_file`, holding one line on what the folder is for, add the category's line to `shared/INDEX.md`, then hand the new index over with `artifact_rehome` (pack `artifacts`; find the artifact's `integration`, `type` and `external_id` with `artifact_resolve`) to the agent's thread id. The agent's writes to its own folder are refused until that file exists, and it can't edit an index someone else owns.
6. For a thread skill, use `kind="skill"`, a unique lowercase `name`, valid `SKILL.md` frontmatter and `content`. Optional `supporting_files` maps one-level `scripts/` or `references/` paths to text; a skill with CORSIA data (see skill `skill-creator`) also includes `corsia.yaml`. Scripts are installed, never run by this tool. The receipt proves the skill appears in the target's next-turn skills list; a shadowed skill is an incomplete install.
7. Keep the returned bundle `revision` for later edits; `absent` protects creation. Retrying the same desired bytes is idempotent. For stale revisions, read current files before changing them. Verify roster, attached responsibility and skill delivery through the target's fresh turn, and that `GET /api/agents` lists it as a visible thread (not a subagent), which is what puts it under Agents in the person's app.
