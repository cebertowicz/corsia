# Offline bootstrap brief: install CORSIA and set up a contributor workspace

**Status:** draft · **Owner:** CORSIA maintainers · **Review:** every release

**Audience:** a Claude Code or Codex session running on the contributor's Mac,
*outside* CORSIA. Follow the steps in order and run each command yourself.
Stop and ask the human only where a step says **ASK**. Every setting cites
the source file that defines it. A **TODO** marks a value the maintainers
have not verified; don't guess a replacement. Leave it out, or ask on the
tracking issue.

**Result:** a running, separate CORSIA instance with one user, one working
provider, and a workspace loaded with the starter kit, ending with a
conductor thread you hand off to. The conductor takes it from there —
project, contributor agent, and smoke test included — following
[`workspace-setup.md`](workspace-setup.md).

Read [`README.md`](README.md) first for how the pack fits together.

---

## 0. Ground rules for this session

- This instance is yours alone. Never point it at, copy data from, or send
  data to the maintainers' instance. The only shared surface is the GitHub
  repository (see [`CROSS-WORKSPACE-PROTOCOL.md`](CROSS-WORKSPACE-PROTOCOL.md)).
- Never commit `.env`, tokens, or `~/.corsia/config.json`.
- Prefer reversible steps. Before overwriting any existing `~/.corsia`,
  **ASK**.

## 0a. When a step fails

Troubleshoot before asking. Diagnose the exact error and try the known fixes
first:

- A binary the service can't see: check the install location against the
  service's `PATH` (section 1, Safehouse row), fix the location, then
  `corsia restart`.
- A provider missing from `GET /api/models`: confirm the CLI is installed and
  signed in, then restart CORSIA.
- A thread that hangs after one tool call, or a model switch that fails: stop
  the thread, re-request the same switch, restart the service; if it repeats,
  record `git -C ~/.corsia rev-parse --short HEAD` and report it.

Mark a step blocked only after those attempts, stating the exact error and
what you tried. A defect in CORSIA itself goes upstream as an issue per
[`CROSS-WORKSPACE-PROTOCOL.md`](CROSS-WORKSPACE-PROTOCOL.md), stated
impersonally — never attributed to a person. **ASK** the human only for what
this session cannot do itself (a TCC prompt, `sudo`, a sign-in, a service-script
edit) or must not decide alone. A host-access lease is not a fix for a missing
`PATH` entry or a missing binary.

## 1. Prerequisites

| Need | Why / source |
|---|---|
| macOS on Apple Silicon | Local inference (`README.md` Quick start) |
| Python **3.11** | The backend contract. The test environment rejects 3.12+ (`starter-kit/project-skills/corsia-test-environment`). `README.md` says "3.11+", but use exactly 3.11. |
| Node **20+** | Frontend build (`README.md`) |
| `git`, `gh` (logged in: `gh auth status`) | Auto-detected at startup; the GitHub connection needs auth (`backend/connections/__init__.py`, `_gh_authed`) |
| [Agent Safehouse](https://github.com/eugene1g/agent-safehouse) **≥ 0.11.1** | The agent `bash` tool fails closed without it, reports the backend `PATH` it searched, and says that nothing ran. Install it, or add its directory to the CORSIA service `PATH` and restart the service (`backend/tools/shell.py`, `backend/tools/shell_guard.py` `_MIN_SAFEHOUSE_VERSION`). The launchd service renders a fixed `PATH` that includes the standalone installer default (`$HOME/.local/bin`) plus Homebrew and system locations (`scripts/corsia.d/service.sh` `SERVICE_PATH`), so either the README's standalone install or `brew install eugene1g/safehouse/agent-safehouse` is visible after service install/start. |
| `agent-browser` CLI | Browser tools: `brew install vercel-labs/tap/agent-browser && agent-browser install` (`backend/tools/browser.py`) |
| `claude` CLI (optional) | Claude Code provider, section 5 |
| `codex` CLI (optional) | Codex provider, section 5 |
| `omlx` (optional) | Local inference, section 5 |
| Brave Search API key (optional) | The `web_search` tool (section 3) |
| Tailscale (optional) | [Private HTTPS phone/PWA and laptop access with Serve](workspace-setup.md#3-reach-it-from-your-phone--mixed) (§3). Public webhook ingress with Funnel is a separate optional setup (§7); it is not needed for phone access. |

## 2. Choose locations (macOS privacy/TCC)

Put the **workspace** somewhere macOS privacy protection (TCC) does not guard,
for example `~/corsia`. Do **not** use `~/Documents`, `~/Desktop`,
`~/Downloads`, or any iCloud Drive folder. Background agents and the launchd
service get TCC permission prompts or silent denials there, and the Safehouse
sandbox denies them by design (`SECURITY.md`, Agent Sandbox).

Three distinct roots, never nested inside each other:

| Root | Default | Source |
|---|---|---|
| Serving checkout (the code that runs) | `~/.corsia` | `scripts/corsia` (reads `.venv` and `.env` from its own checkout); `README.md` Quick start |
| Workspace (your files, context, skills, projects) | chosen at `init`, stored in `~/.corsia/config.json`; overridable with `CORSIA_WORKSPACE` | `backend/infra/config.py` (`BOOTSTRAP_CONFIG_PATH`, `_resolve_workspace`), `backend/cli.py` `init` |
| Operational state (database, logs) | `~/Library/Application Support/<app id>/state`; overridable with `CORSIA_STATE_DIR` | `backend/infra/platform_paths.py` `default_state_root` |

**ASK** the human to confirm the workspace path before creating it.

## 3. Install the serving checkout

```bash
git clone <upstream repo URL> ~/.corsia      # the repo containing this file
cd ~/.corsia && git checkout main             # release branch; development lands on dev
python3.11 -m venv .venv
.venv/bin/python -m pip install -r backend/requirements.lock
(cd frontend && npm ci && npm run build)
cp .env.example .env                           # stays in the checkout; never commit
```

- `scripts/corsia` runs the backend with `~/.corsia/.venv/bin/python` and
  loads `~/.corsia/.env` (`scripts/corsia`, `PYTHON=` / `ENV_FILE=`).
- The maintainers install no symlink and invoke `scripts/corsia` by its full
  path; the project's own skills and runbooks all refer to it that way. If you
  prefer a short command, any directory already on your `PATH` works, for
  example `ln -s ~/.corsia/scripts/corsia /usr/local/bin/corsia`. Nothing
  depends on the symlink existing. The script only operates its own checkout
  (`scripts/corsia`, the `CORSIA_DIR` check).
- **Web search:** the person enters the Brave Search key in Settings › Keys
  (entry `web-search`); it is not read from `.env` or the environment. Each
  search reads it afresh, so no restart is needed (`backend/infra/key_store.py`).
- **Cloud API keys** (optional, for the native loop) are entered in Settings ›
  Providers and saved in the key store (`backend/infra/key_store_providers.py`),
  not in `.env`. An `ANTHROPIC_API_KEY` or `OPENAI_API_KEY` in the environment
  is ignored; Settings says so on the provider's card.
- **Service label:** the launchd job defaults to `com.corsia.prod`. Set
  `CORSIA_SERVICE_LABEL` in `.env` only if this Mac already runs another
  CORSIA instance (`scripts/corsia.d/service.sh`).

Initialize the workspace (it scaffolds the directory and writes
`~/.corsia/config.json`):

```bash
cd ~/.corsia && .venv/bin/python -m backend.cli init --path <workspace> --name <workspace name>
```

Source: `backend/cli.py` `cmd_init`. It refuses to overwrite an existing
config without `--force`, and `--force` cannot move the roots.

## 4. Sign-in and agent access

> **Reading these commands.** Some agent file readers mask credential-shaped
> lines, so an `Authorization: Bearer …` example below may come back as
> `[REDACTED]`. The file is intact. Never paste a masked read-back into an
> edit. To see the real bytes: `sed -n '<line>p' <file> | od -c`.

Check which path applies: `git -C ~/.corsia merge-base --is-ancestor f98af18a HEAD`
exits 0 when your checkout includes the sign-in change. Use **4A** if it
does and **4B** if it does not.

### 4A. Requires CORSIA at or after `f98af18a`

**Human setup.** `corsia start` (section 5) asks for the workspace path, a
username, and a password on first run. **ASK** the human to type the
password themselves; never choose, see, or store it. The human then signs in
at `http://<host>:1956`. Sessions last 30 days.

**Accounts.**

| Command | Does |
|---|---|
| `corsia user add` | Create another account |
| `corsia user passwd` | Change an account's password |
| `corsia user list` | List accounts |

**Single-owner model.** A second account gets its own chats, plans, and
attention items but shares everything else: workspace, projects, files,
settings, and providers. There are no roles or permissions yet, so add only
people you would give full access to.

**Network.** The backend listens on all interfaces on port 1956. Keep the Mac
on a trusted network or a private tailnet, never an open or public network.

**Agent access.** Agents use an API token, never a password.

1. The **human** runs `corsia token create --label agents --write-delegate-file`.
   It writes the token to `~/.corsia/.api-token` with mode `600`. The agent
   never sees or copies the value.
2. The `corsia` CLI and helpers read that file. The `CORSIA_API_TOKEN`
   environment variable overrides it.
3. Raw HTTP calls send the header `Authorization: Bearer <token>`, read from
   the file at call time. Never put a token in a URL (`?token=`): URLs end up
   in logs and shell history.

**Token rules** (the starter kit delivers these to every agent in
`context/CORSIA-API-ACCESS.md`):

- Never paste a token into chat, files, issues, PRs, or commits.
- Agents never use a password and never call `/api/auth/login`.
- On HTTP 401, stop and ask the human to reissue the token. Do not retry.
- Tokens do not expire. `corsia token list` shows them;
  `corsia token revoke <label>` revokes one.

### 4B. Older builds (before `f98af18a`)

> **Gap.** CORSIA has no user-creation command, API, or UI yet. Users and
> their bearer tokens are a static dict, `USERS`, in
> `backend/infra/config.py`, looked up by `backend/infra/auth.py`
> `authenticate`. The dict ships with the maintainers' development entries,
> and those tokens are public to anyone with the source.

Until that changes:

1. Generate a random token: `python3 -c "import secrets; print(secrets.token_urlsafe(32))"`.
2. In your **serving checkout only**, replace the entries in `USERS` with one
   entry, shaped like the existing ones:
   `"<token>": {"id": "<your user id>", "name": "<display name>", "profiles": ["personal"]}`.
   This is a local, uncommitted change. Never push it. Remove the shipped
   entries so their public tokens cannot authenticate to your instance.
3. Store the token for the CLI: write it to `~/.corsia/.api-token`, mode
   `600`. `corsia delegate|doc|context` read it there
   (`backend/scripts/context_cli.py` `--token` default,
   `scripts/corsia.d/delegate.sh`).
4. The web UI and API accept the token as `Authorization: Bearer <token>` (`backend/infra/auth.py`).
   The code also accepts `?token=`; never use it (see 4A).

Because `git pull` in the serving checkout will conflict with this local
edit, keep it as a single-line change and reapply it after updates. Report
friction on the tracking issue (section 11).

## 5. Start, then configure providers

```bash
corsia start          # 4A builds: asks for workspace, username, password
# or: corsia service install && corsia service start
corsia status
curl -s http://127.0.0.1:1956/api/health    # expects {"status": "ok"}
```

Sources: `scripts/corsia` usage text; `backend/api/system.py` `/api/health` (not `/health`, which the frontend's catch-all route answers).
The default port is 1956 (`scripts/corsia`).

At startup CORSIA auto-detects three CLI connections. It looks on `PATH`,
then in `~/.local/bin`, `/opt/homebrew/bin`, and `/usr/local/bin`
(`backend/connections/__init__.py`):

| Provider | Setup | Ready when |
|---|---|---|
| **Claude Code subscription** | Install `claude` (see the note below) and log in with the subscription account, then restart CORSIA | Connection `claude-code` is available; `claude-code/*` rows in `GET /api/models` show `status: "ready"` |
| **Codex subscription** | Install `codex`, then `codex login` | `codex login status` exits 0 (`_codex_sign_in_state` returns `ready`; a signed-out answer is `needs_auth`, a timeout or other failure `unverified`); `codex/*` rows show `ready` |
| **GitHub** | `gh auth login` | `gh` authenticated (`_gh_authed`) |

**Where `claude` has to be.** The official installer puts it at
`~/.local/bin/claude` on macOS and Linux, and Homebrew offers a `claude-code`
cask; both land somewhere the startup scan above already covers, so a normal
install needs no PATH work. The scan records the resolved path as the
`claude-code` connection's `config.binary`, and the runtime launches exactly
that (`backend/connections/__init__.py`,
`backend/subagents/cc_runtime_resume.py` `_resolve_binary`).

The one trap: the VS Code extension bundles a private copy of the CLI in its
own extension directory, which is in none of the scanned locations. If that is
your only install, the connection stays `not_installed` and no `claude-code/*`
model row appears. Run the standalone install as well.

**oMLX local server.** Install oMLX from <https://github.com/jundot/omlx>.
The maintainers currently run **0.7.0.dev2**, read from `GET /api/status` on
the running oMLX server. Older versions are untested here rather than known
bad. Then either:

- let CORSIA manage it: CORSIA spawns `omlx serve` for a managed
  network-server row (`backend/llm/providers/omlx_supervisor.py`); or
- register a running server: `POST /api/settings/network-servers` with
  `{"name", "hostname", "port"}` (default 8000) and an optional `api_key`
  (`backend/api/network_servers.py` `AddServerBody`), or use **Settings → Providers**
  (`frontend/src/lib/components/SettingsView.svelte`).

Local rows then appear in `GET /api/models` with `provider: "omlx"`,
`backend: "network"`, and a `litellm_model` shaped
`omlx@<namespace>/<model-id>` (`backend/llm/local_backends.py`,
`backend/llm/model_status.py`).

## 6. Load the starter kit templates into the workspace

The System context (intent and authority, context and work state,
verification, human-agent interface, host-access boundary, CORSIA API access,
spawn rules, the agent and conductor role contracts) and the core skills ship
inside CORSIA (`backend/prompts/system/v1/`, loaded by `backend/core/system_tier.py`).
They need no install and cannot be edited from the workspace. Copy only the
workspace templates; the platform discovers them by directory:

| Copy from `docs/contributing/bootstrap/starter-kit/` | To | Loaded by |
|---|---|---|
| `context/SPAWN-POLICY.md` | `<workspace>/context/` | Scanned as workspace context (`backend/core/workspace_context_sources.py`) |
| `skills/*` | `<workspace>/skills/` | Workspace skills; each `SKILL.md` needs `name` and `description` frontmatter (`backend/core/skills_registry.py`) |
| `shared/standards/*` | `<workspace>/shared/standards/` | Referenced by the skills and role contracts |
| `project-skills/*` | `<workspace>/projects/corsia-contrib/skills/` (the conductor copies these in when it creates the project) | Project skills for work on the CORSIA repo |

Then fill in every `<…>` placeholder in `<workspace>/context/SPAWN-POLICY.md`
with the models that show `ready` on *this* instance. Delete rows for
providers you did not configure.

## 7. Create the conductor, then hand off

**Use a project, not the workspace root.** Everything past this point —
cloning a worker copy of the repository, creating the `corsia-contrib`
project, and the contributor agent — happens inside a project, where
protected base checkouts, branch pinning, and `worktree_create` actually
work (`project_repo_policies.project_id` is `NOT NULL`,
`backend/infra/schema.py`). That is the conductor's job, next.

1. **Open the workspace conductor from the rail.** The first account creates
   the workspace conductor automatically. `PUT /api/conductor` remains the
   re-designation API if that conductor ever needs to be replaced.
2. **Hand off.** Tell the human: open that thread and ask it to run the
   `conductor-bootstrap` skill, naming this as a contributor workspace so it
   also follows [`contributor-workspace.md`](contributor-workspace.md) in this directory. From there the
   conductor creates the project, registers the repository, writes the
   charter and coordination model, creates the contributor agent, sets the
   model settings, and runs its own smoke test — the exact commands are in
   [`workspace-setup.md`](workspace-setup.md).

## 8. Report back

Report setup friction, tool errors, and gaps you hit up to this handoff on
the upstream tracker, following
[`CROSS-WORKSPACE-PROTOCOL.md`](CROSS-WORKSPACE-PROTOCOL.md) §6. Search first,
and comment on an existing issue instead of filing a duplicate. The
conductor reports its own half the same way — see `workspace-setup.md` §
Report upstream.
