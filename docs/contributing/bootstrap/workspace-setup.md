# Contributor workspace setup (conductor side)

**Audience:** the conductor thread in a freshly created contributor
workspace, right after [`OFFLINE-BOOTSTRAP.md`](OFFLINE-BOOTSTRAP.md)
hands off to you. Follow `contributor-workspace.md` for the shape of the
project and roster; this file has the exact commands and settings.

**Run it / walk it.** Every machine-level step below is marked **run it**
(you do this yourself, with your own tools) or **walk it** (tell the human
the exact action and why you cannot do it, then wait). Anything needing
`sudo`, anything that writes or loads a launchd job, and anything that
needs the human's own account sign-in, browser session, or a native macOS
window is walk it. Everything else is run it.

## 0. When a step fails — run it

Troubleshoot before asking. Diagnose the exact error and try the known fixes
first, then mark a step blocked only after those attempts, stating the exact
error and what you tried.

Your own `bash` tool runs inside this CORSIA instance and needs Agent
Safehouse on a `PATH` the launchd service can see — a terminal-only install
is not enough. If `bash` fails with "shell execution requires Agent
Safehouse" even though `safehouse --version` works for a human in a
terminal, that is this exact problem: the service's `PATH` doesn't reach the
binary. You cannot fix your own service's `PATH` or reinstall a binary
outside your sandbox, so this one is **walk it** — tell the human the exact
command to install Safehouse where the service can see it (Homebrew,
`brew install eugene1g/safehouse/agent-safehouse`, or the standalone script
placed in `/usr/local/bin`; see `OFFLINE-BOOTSTRAP.md` §1) and to run
`corsia restart` afterward, then wait.

Other failures stay **run it**: a provider missing from `GET /api/models` —
confirm the CLI is installed and signed in, then restart; a thread that
hangs after one tool call, or a model switch that fails — stop it, re-request
the same switch, restart the service, and if it repeats, record
`git -C ~/.corsia rev-parse --short HEAD` and report it. A defect in
CORSIA itself goes upstream as an issue per `CROSS-WORKSPACE-PROTOCOL.md`
(§9 below), stated impersonally. Ask the human only for what you cannot do
yourself (a TCC prompt, `sudo`, sign-in, a launchd/service-script edit) or
must not decide alone. A host-access lease is not a fix for a missing `PATH`
entry or a missing binary.

## 1. Recommended model settings — run it

These are the maintainers' current operating values, read from their live
instance through the endpoints named here. Apply them through the same
endpoints on your instance. `{model_id}` is the row's catalog `id` from
`GET /api/models`, not its `litellm_model`. URL-encode it
(`claude-code%2Fopus%5B1m%5D`). Writes need the bearer token.

**Context caps.** Set with
`PATCH /api/models/{model_id}/profile` and body `{"context_window": N}`
(`backend/api/models.py` `update_model_profile`). For Claude Code and Codex
rows, the value can only *lower* the discovered window
(`backend/llm/context_window.py` `resolve_window_for_row`).

| Model rows | `context_window` | Why |
|---|---|---|
| Claude Code 1M-context rows (`claude-code/opus[1m]` etc.) and `claude-code/sonnet` | `262144` | 1M seats fold (checkpoint) at a 262k operator cap |
| Mechanical tier (`claude-code/haiku`, the Codex mechanical tier) | `131072` | 128k cap; give these models bounded briefs |
| Local Qwen3.8 Flash Next oQ6 | `262144` | The maintainers' live profile value |

**Local model.** The maintainers run Qwen3.8 Flash Next **oQ6** (MTP build)
on a 256 GB M3 Ultra. Measured on that machine with oQ6 loaded and idle:
the model occupies about **111 GiB**, against a memory-guard ceiling of about
**243 GiB** (`current_model_memory` and `final_ceiling` from `GET /health` on
the oMLX server). So oQ6 uses roughly 45% of the ceiling and leaves ample room
for context.

Those are two numbers from one machine, not a sizing table. Smaller RAM has
not been tested here, and the figure to watch is the model footprint plus your
working context against the ceiling, which scales with installed memory. oQ4
and oQ8 variants exist; oQ4 is the one to try first on a smaller machine.
Check your own numbers with `GET /health` after loading. Load **one variant at
a time**. Always dispatch with the exact `litellm_model` that
`GET /api/models` reports for the row whose `status` is `ready`
(`available` means on disk, not loaded). The maintainers' selector looks like
`omlx@<ns>/Qwen3.8-Flash-Next-MLX-oQ6-MTP`, but the `<ns>` part differs on
every install, so never copy it.

**Sampling for Qwen3.8 Flash Next oQ6.** Set these in oMLX's model settings.
CORSIA does not read, store, or apply oMLX sampling controls.

| Parameter | Value |
|---|---|
| temperature | 1.0 |
| top_p | 0.95 |
| top_k | 20 |
| min_p | 0.0 |
| repetition_penalty | 1.0 |
| presence_penalty | 0.0 |
| max_tokens | not overridden; the global default 8192 applies (`backend/llm/inference_params.py` `_GLOBAL_DEFAULTS`) |

oMLX ignores `repetition_context_size`
(`backend/llm/inference_params.py`).

**oMLX server-level settings.** These are set on the oMLX server itself, not
through CORSIA (oMLX admin UI, or `GET`/`POST /admin/api/global-settings`).
The maintainers' current values on a 256 GB M3 Ultra:

| Group | Setting | Value |
|---|---|---|
| memory | `prefill_memory_guard` | `true` |
| memory | `memory_guard_tier` | `aggressive` |
| memory | `memory_guard_custom_ceiling_gb` | `0` (unset; the tier decides) |
| scheduler | `max_concurrent_requests` | `8` |
| scheduler | `chunked_prefill` | `true` |
| scheduler | `prefill_priority` | `context` |
| scheduler | `decode_fairness` | `true` |
| scheduler | `embedding_batch_size` | `32` |
| cache | `enabled` | `true` |
| cache | `hot_cache_max_size` | `128GB` |
| cache | `initial_cache_blocks` | `4096` |
| cache | `gdn_ssd_split_enabled` | `true` |
| cache | `gdn_sidecar_precision` | `fp32` |
| cache | `ane_compile_cache` | `false` |
| server | `burst_decode_mode` | `aggressive` |
| server | `preserve_mid_system_cache` | `true` |

Scale the cache sizes to your own disk and memory; `hot_cache_max_size` of
128 GB assumes 256 GB of RAM. The `aggressive` memory-guard tier suits a
single-user machine running one model; lower it if you share the box.

**Host-access reviewer.** `PATCH /api/settings` with
`{"host_access_reviewer_model": "<exact ready model id>"}`. Optionally add
`{"host_access_auto_approve": true|false}`: `false` sends each approved lease
to you for a decision (`backend/api/system.py`). Pick a model you will
**not** use for ordinary delegation, so its quota stays available.

## 2. Create the project, and the contributor agent — run it

**Use a project, not the workspace root.** Protected base checkouts, branch
pinning, and `worktree_create` exist only inside a project:
`project_repo_policies.project_id` is `NOT NULL` and references `projects`
(`backend/infra/schema.py`). `project_repo_register` returns "requires a
current project" (`backend/tools/project_repo_register.py`), and
`worktree_create` returns "worktree creation requires a current project"
(`backend/tools/worktrees.py`). Working at the workspace root would edit an
unprotected checkout.

1. **Clone a worker copy** of the repository, separate from the serving
   checkout: `git clone <upstream repo URL> <workspace>/repos/corsia`, then
   `git -C <workspace>/repos/corsia checkout dev`. Never develop in
   `~/.corsia`.
2. **Create the project** `corsia-contrib` with `POST /api/projects`
   (`backend/api/projects.py` `create_project`), or with the `project_create`
   tool. Pass `repo_path` as the worker clone. The project links it under
   `source/` and never moves it.
3. **Register the repo** if it is not listed yet: call `project_repo_register`
   with `repo_path`. It pins the current branch (`dev`) and protects the
   base checkout. Confirm with `project_repos`.
4. **Open the project conductor from the rail.** The project's conductor exists
   as soon as the project does. Open it from the rail; `PUT /api/conductor`
   remains the re-designation API if the project conductor ever needs to be
   replaced.
5. **Create the contributor agent:** one thread in the project, promoted with
   `PATCH /api/agents/{thread_id}` and body
   `{"agent_enabled": true, "responsibility": "<one line>"}`
   (`backend/api/agents.py`). Its bootloader comes from
   `starter-kit/templates/RESPONSIBILITY.md`, saved as the thread's
   `.threads/<id>/context/RESPONSIBILITY.md`, which loads automatically
   (`backend/core/role_context_sources.py`). You can also create it with
   `corsia context create thread <thread id> RESPONSIBILITY.md` and attach an
   existing file with `corsia context attach thread <thread id> <path>`
   (`backend/scripts/context_cli.py`).

Write the charter and coordination model per `conductor-bootstrap`'s own
steps 2–3, using `contributor-workspace.md`'s shape and the coordination-model
table it gives you.

## 3. Reach it from your phone — mixed

Use [Tailscale Serve](https://tailscale.com/docs/features/tailscale-serve)
to give CORSIA a private HTTPS address for your phone and other laptops.
Access stays within the tailnet and follows its access rules. This setup
does not require a Tailscale Service such as `svc:corsia`, tags,
service-host approval, or Funnel. Enabling Serve does not interrupt an
existing screen-sharing connection over Tailscale.

### Private HTTPS and Home Screen setup

The verified example below assumes CORSIA is already running locally at
`http://127.0.0.1:3001`. Use your actual backend port if different; CORSIA's
default is 1956. Keep CORSIA running on the host while using it remotely.

1. **Walk it.** Connect the CORSIA host and phone to the same Tailscale
   tailnet. Another laptop will need to be connected to that tailnet too.
2. **Run it, on the CORSIA host.** Check `tailscale serve status` first.
   If port 443 already serves another endpoint or the public webhook Funnel
   from §7, preserve it and use the separate-port option below. Otherwise:

   ```bash
   tailscale serve --bg http://127.0.0.1:3001
   ```

   On macOS, if `tailscale` is not on `PATH`, use the app's CLI:

   ```bash
   /Applications/Tailscale.app/Contents/MacOS/Tailscale serve --bg http://127.0.0.1:3001
   ```

   Use that same executable for the other `tailscale` commands here when
   needed. `--bg` keeps Serve configured after the terminal closes.
3. **Walk it on first use, then run it again.** If Tailscale prints
   **“Serve is not enabled”** and an approval URL, open that URL and enable
   Serve. The consent flow enables the required tailnet HTTPS support;
   this is not Service-host approval. Then run the same Serve command
   again. Keep the actual approval URL private.
4. **Walk it.** Tailscale returns a URL shaped like
   `https://<device>.<tailnet>.ts.net/`. Open the returned URL on the phone
   with Tailscale connected, and sign in to CORSIA with the same account
   used on the desktop (§4A of `OFFLINE-BOOTSTRAP.md`). The HTTPS address
   has its own browser session, so a local desktop sign-in does not carry
   over automatically.
5. **Walk it.** In **iPhone Safari**, choose **Share → Add to Home Screen**
   to install the Home Screen app. In Android Chrome, use **Install app**
   when offered. The same HTTPS URL also opens CORSIA in a browser on
   another tailnet-connected laptop.

The URL uses generic placeholders here; use the URL printed on your host.
**Run it.** To disable this private HTTPS listener, run on that host:

```bash
tailscale serve --https=443 off
```

Command details: [Tailscale Serve CLI](https://tailscale.com/docs/reference/tailscale-cli/serve).

### When port 443 is already in use, or adding public webhooks later

Funnel in §7 is optional and only for public webhook ingress. Serve and
Funnel cannot provide private and public handlers on the same port.
**Run it.** If 443 is already in use, choose an unused private port such as 8443:

```bash
tailscale serve --bg --https=8443 http://127.0.0.1:3001
tailscale serve status
```

**Walk it.** Read the status output with the human before opening it: 8443 must be
tailnet-only. The phone or laptop then uses
`https://<device>.<tailnet>.ts.net:8443/`; disable that listener with
`tailscale serve --https=8443 off`.

**Run it.** If adding §7's Funnel later, first move the private app proxy to 8443 and
remove its old 443 listener with `tailscale serve --https=443 off`. Verify
the private app is reachable on 8443 before continuing to §7. Never leave
the app's root handler on a port you are about to make public.

### Existing HTTP access and app assets

`corsia status` also prints a plain HTTP Tailscale address
(`scripts/corsia.d/runtime.sh`). It remains usable for browser access;
HTTPS is the setup above for app installation. Chrome does not offer PWA
installation over the plain HTTP tailnet address.

The HTTPS page uses the existing PWA manifest: `frontend/static/manifest.json` sets
`"display": "standalone"` and `"display_override"` including `"standalone"`,
with 192/512/1024px icons. iOS specifically uses the `apple-touch-icon` link
in `frontend/src/app.html`
(`<link rel="apple-touch-icon" sizes="180x180" href="/icons/corsia-apple-touch-180.png" />`),
not the manifest icons, for its home-screen icon.

## 4. Install the desktop app — mixed

`desktop/` is a Tauri 2 shell (`desktop/src-tauri/tauri.conf.json`,
`Cargo.toml`: `tauri = "2.10.3"`, `tauri-build = "2.5.6"`). It renders no
CORSIA UI itself — it is a native macOS window (traffic-light overlay,
sidebar vibrancy, draggable title bar) that loads whatever server URL you
give it. On first launch it shows a small picker page
(`desktop/dist-shell/index.html`); the URL you paste there is persisted to
the app's own local config file and reused on every later launch
(`desktop/src-tauri/src/lib.rs`: `set_server_url`, `get_server_url`,
`config_path` — under the identifier `com.cebertowicz.corsia` from
`tauri.conf.json`). There is a second, `dev`-labeled bundle config,
`tauri.dev.conf.json` (identifier `com.cebertowicz.corsia.dev`), for
running a development build side by side with a release one.

**Run it.** Building follows the ordinary Tauri 2 CLI (`cargo install
tauri-cli`, then `cargo tauri build` run against `desktop/src-tauri`, or
`cargo tauri dev` with `--config tauri.dev.conf.json` for the dev-labeled
build). No `sudo` is needed.

**Not verified — left out.** The repository has no packaged build script,
CI workflow, or written release/signing/distribution instructions for this
app (no `desktop/package.json`, no workflow references `tauri`). The command
above is the standard Tauri 2 build path implied by `Cargo.toml` and
`tauri.conf.json`, not a documented repo procedure — treat it as a starting
point and report friction (§9).

**Walk it.** Open the built app and paste the same URL you used for phone
access above (`http://localhost:<port>` on this Mac, or the tailnet address
from §3 for another machine) into the first-launch picker. This is a native
window; nothing here is reachable through a browser tool.

## 5. launchd service configuration — walk it

**Everything in this section is walk it.** Agents cannot write launch-agent
plists under CORSIA's own safety boundary, even with a host-access lease —
every `corsia service` subcommand that touches launchd is something you
tell the human to run, not something you run yourself.

`corsia service install` writes
`~/Library/LaunchAgents/<label>.plist`, where `<label>` is
`CORSIA_SERVICE_LABEL` from the checkout's `.env`, default `com.corsia.prod`
(`scripts/corsia.d/service.sh`: `LAUNCH_AGENTS_DIR`, `service_label`,
`service_plist_path`; label characters are restricted to letters, digits,
`.`, `-`, `_`). The rendered plist (`render_service_plist`) sets:

- `ProgramArguments`: `$PYTHON -m uvicorn main:app --host 0.0.0.0 --port
  $BACKEND_PORT` — the same backend, always bound to all interfaces;
- `WorkingDirectory`: the serving checkout's backend directory;
- `EnvironmentVariables`: `CORSIA_SERVICE_GENERATION`, `BACKEND_PORT`,
  `PATH`, plus every variable from the checkout's `.env`
  (`render_env_file_vars`);
- `KeepAlive` and `RunAtLoad`: `true`;
- `StandardOutPath` / `StandardErrorPath`: the same backend log path used
  elsewhere in this pack.

Tell the human what to check or set:

- **Label collision.** If this Mac already runs another CORSIA instance,
  set a distinct `CORSIA_SERVICE_LABEL` in this checkout's `.env` before
  installing (already noted in `OFFLINE-BOOTSTRAP.md` §3).
- **Port collision.** `BACKEND_PORT` in `.env` controls the port in the
  plist; changing it needs a reinstall/restart, not a live edit.
- **`.env` edits need a restart, not a reload.** `corsia service restart`
  re-renders the plist from the current `.env` and reloads it
  (`service_restart` calls `service_install restart`), so it is the command
  that picks up a change like the reviewer model in §6 below. There is no
  separate "reload config" command.
- **Debugging.** The log path printed by `corsia service install` (and
  reused by `restart`) is where `stdout`/`stderr` for the service land.

## 6. Reviewer model — walk it (the one step that needs a user action)

CORSIA's LLM PR reviewer resolves its model from `CORSIA_REVIEWER_MODEL`
(fallback: `CORSIA_REVIEWER_FALLBACK_MODEL`), read from
`~/.corsia/.env`. If they are unset, it reads the settings
`agents.defaults.reviewer_model` / `reviewer_fallback_model`, which have no
API write path. There is no built-in default
(`backend/reviewer/reviewer_runner.py`).

**Tell the human exactly this:** add `CORSIA_REVIEWER_MODEL=<ready model
id>` (and optionally `CORSIA_REVIEWER_FALLBACK_MODEL=<id>`) to
`~/.corsia/.env`, then run `corsia service restart` (§5 above) so the new
value is loaded. This is walk it because `~/.corsia/.env` is the serving
checkout's own env file, outside any project workspace you can reach with a
supported tool, and because the ground rule in `OFFLINE-BOOTSTRAP.md` §0
("never commit `.env`, tokens...") makes editing it a human-only action by
design — the same reason the human, not you, types their own sign-in
password in §4A.

The reviewer runs from a GitHub webhook (`POST /webhooks/github`), which
rejects deliveries until the settings row `webhook` has a
`github_webhook_secret` (`backend/api/webhooks.py`). §7 sets up that
delivery path.

**The upstream repository's `qa/llm-reviewer` status belongs to the
maintainers.** Configure a reviewer only for repositories you own. Never
publish reviewer statuses on the upstream repository. For your own
pre-push review, see `project-skills/ci-gates-locally`.

## 7. Public ingress for GitHub webhooks (Tailscale Funnel) — mixed

Skip this whole section if you are not running a reviewer and do not need
event-driven PR work. CORSIA is fully usable without it; you just poll
GitHub yourself instead of being woken by it.

GitHub has to reach your machine to deliver events, and a laptop behind a
router has no public address. Tailscale Funnel gives one node a public HTTPS
URL that tunnels to a local port, with a TLS certificate provisioned for you
and your home IP address never exposed. CORSIA already assumes Tailscale is
in play — `scripts/corsia.d/runtime.sh` prints a Tailscale endpoint after
startup (§3 above) — so it is the path of least resistance here.

### Expose only the webhook path

Funnel's default is to publish the *whole* service on that port. Do not do
that here: port 1956 is the entire CORSIA app and API, and publishing it
puts your agents, threads, and workspace on the public internet behind
nothing but a bearer token. Publish only the one path GitHub posts to.

**Before enabling Funnel on 443:** if §3 already installed the private app
proxy there, follow its separate-port steps to move that proxy to 8443 and
remove the private 443 listener first. The public port must have only the
webhook handler, never the app's root handler.

1. **Walk it.** Install Tailscale (v1.38.3 or later) and sign in:
   <https://tailscale.com/download>. Funnel needs the Tailscale **CLI**. Every
   macOS variant can share a port, which is all this needs; only file and
   directory serving requires the open-source variant
   (<https://tailscale.com/docs/concepts/macos-variants>). If `tailscale` is
   not on `PATH`, the binary is inside the app bundle at
   `/Applications/Tailscale.app/Contents/MacOS/Tailscale`. This needs the
   human's own Tailscale account.
2. **Walk it.** In the admin console, enable **MagicDNS** and **HTTPS
   certificates** for the tailnet, and add the `funnel` node attribute to the
   policy file. Funnel requires all three
   (<https://tailscale.com/docs/features/tailscale-funnel>). A web-console
   action on the human's own account.
3. **Walk it (`sudo`).** Publish just the webhook path, not the whole app:

   ```bash
   sudo tailscale funnel --bg --set-path=/webhooks/github 1956
   tailscale funnel status   # prints the mount table and your public URL
   ```

   `--set-path` mounts one URL path; `--bg` keeps it running after the shell
   exits. Funnel's HTTPS listener may only use ports 443, 8443, or 10000 (the
   listener port, not your local 1956), and proxy targets must be on
   `127.0.0.1` (<https://tailscale.com/docs/reference/tailscale-cli/funnel>).

4. **Run it.** Prove the mapping before you trust it. The maintainers could
   not run these commands on the machine this pack was written on, and
   whether a mounted prefix is passed through to the local service or
   stripped is the one detail worth checking yourself:

   ```bash
   curl -sS -X POST https://<node>.<tailnet>.ts.net/webhooks/github \
     -H 'X-GitHub-Event: ping' -H 'Content-Type: application/json' -d '{}'
   ```

   Expect `{"status":"ok","event":"ping"}`. This unsigned probe is accepted
   only because no secret is configured yet; once you set one below, CORSIA
   verifies signatures on pings too. A `404` means the prefix was stripped
   before it reached CORSIA; re-run step 3 with the path on the target
   instead — `sudo tailscale funnel --bg --set-path=/webhooks/github
   http://127.0.0.1:1956/webhooks/github` — and probe again.

5. **Walk it.** Check from off-network (a phone on cellular is enough) that
   `https://<node>.<tailnet>.ts.net/` does **not** serve the CORSIA UI. If it
   does, you published the whole app: take it down with
   `sudo tailscale funnel --https=443 off` and redo step 3 before continuing.

### Register the webhook on your repository

**Walk it.** On a repository **you own** (never the upstream one): Settings
→ Webhooks → Add webhook (a GitHub web UI action on the human's account).

| Field | Value |
|---|---|
| Payload URL | `https://<node>.<tailnet>.ts.net/webhooks/github` |
| Content type | `application/json` |
| Secret | a fresh random string you generate, e.g. `openssl rand -hex 32` |
| Events | "Let me select individual events": **Pull requests**, **Check suites**, **Issue comments**, **Pull request review comments** |

Those four are the ones CORSIA maps to internal events
(`backend/events/github_mapper.py`): pull request `opened` / `synchronize` /
`closed`-and-merged, `check_suite` `completed`, and `created` comments on
issues and PR reviews. `check_run` is explicitly ignored, and `push` and
`pull_request_review` are not mapped at all — selecting them only adds
deliveries that land in the log and route nowhere. The endpoint accepts and
de-duplicates any delivery by GitHub's delivery id, so over-selecting is
harmless, just noisy.

GitHub sends a `ping` when you save the webhook. That one event is allowed
through unsigned, but only while no secret is configured yet
(`backend/api/webhooks.py`, `_UNSIGNED_ALLOWED_EVENTS`).

### Give CORSIA the same secret

**Run it.** CORSIA reads it from the settings row `webhook`, field
`github_webhook_secret` (`backend/api/webhooks.py` `_get_webhook_secret`).
Every other delivery is rejected until it matches what GitHub signs with.

**There is no supported interface that writes this value** — no API route, no
CLI command, no settings screen. That gap is tracked as issue #1832. Until it
lands, call the backend's own settings helper. Do not edit the database file
by hand.

The one real hazard is writing to the *wrong* database: `default_state_root`
picks a platform default unless `CORSIA_STATE_DIR` is set
(`backend/infra/platform_paths.py`), so a shell that does not load your `.env`
can silently create a second, unused database. Load the same env the server
loads, and print the resolved path before trusting the write:

```bash
cd ~/.corsia/backend
set -a; . ~/.corsia/.env 2>/dev/null; set +a
~/.corsia/.venv/bin/python -c "
from infra import config
print('writing to:', config.DB_PATH)
from infra.settings import save_setting_merge, get_setting
save_setting_merge('webhook', {'github_webhook_secret': 'PASTE_THE_SAME_SECRET'})
print('stored:', bool(get_setting('webhook').get('github_webhook_secret')))
"
```

Confirm the printed path is the database your running instance uses.
`get_setting` reads the row on each request with no caching, so the change
takes effect on the next delivery — no restart needed.

**Walk it.** Verify end to end: in GitHub's webhook page, open **Recent
Deliveries**, redeliver the `ping`, and confirm a `200` whose body contains
`"status": "ok"`. Once a secret exists, CORSIA verifies the signature on
pings too (`backend/api/webhooks.py`), so a redelivered ping is a genuine
test of both halves. A `401` means the two secrets differ. Nothing arriving
at all means the Funnel path is wrong, not the secret.

Treat that secret like a password: it is what stops anyone who finds your
public URL from injecting events into your instance.

## 8. Smoke test — run it

Run all of these. Each line must pass before you report success.

1. `curl -s http://127.0.0.1:1956/api/health` returns `{"status": "ok"}`.
2. `curl -s -H "Authorization: Bearer $(cat ~/.corsia/.api-token)" http://127.0.0.1:1956/api/models`
   lists at least one row with `status: "ready"` for each provider you set up.
3. **4A:** `corsia token list` shows the `agents` label, and
   `stat -f %Lp ~/.corsia/.api-token` prints `600`. **4B:** the shipped
   development tokens return HTTP 401 (step 4B.2 removed them).
4. A new thread in `corsia-contrib` answers a one-line prompt, and its
   context panel (the effective context snapshot) lists the `context/*.md`
   files, the agent role contract (for the agent thread), and the project
   skills.
5. In that thread, the `bash` tool runs `git -C <worker clone> status`, which
   proves Safehouse works.
6. `project_repos` lists `corsia` with `pinned=dev protected`.
7. `worktree_create` with `repo_name: corsia`, `branch:
   smoke/<date>`, `descriptor: smoke` returns a worktree path. Then remove the
   worktree with `git worktree remove <path>` from the worktree's parent.
   A stray local branch ref is harmless.
8. `web_search` returns results, if you set a Brave key.
9. `gh issue list --limit 1` works against the upstream repository.
10. A delegated `claude-code/haiku` (or other ready mechanical-tier) subagent
    answers a one-line task.

## 9. Report upstream — run it

Report setup friction, tool errors, and gaps on the upstream tracker,
following [`CROSS-WORKSPACE-PROTOCOL.md`](CROSS-WORKSPACE-PROTOCOL.md)
§6. Search first, and comment on an existing issue instead of filing a
duplicate. This covers everything from this file — model settings, project
and contributor-agent creation, phone/desktop access, the launchd service,
the reviewer, and the webhook — not just the offline half.
