# CLI reference

Generated reference for commands dispatched by `corsia`.

Generate with `python scripts/docs/generate_cli.py`; add `--check` to compare the generated page. The backend's locked dependencies are required.

The desktop app routes some commands directly to the Python CLI and forwards the rest to the shell dispatcher. Availability is noted for each command.

## Commands

## `agent`

CORSIA agent helpers

Routed in source installs and the desktop app.

### `corsia agent`

### `corsia agent warm`

Pre-warm the CORSIA agent's first reply on the local starter model

## `artifact`

Artifact registry lifecycle (resolve/register/update/reconcile/relocate/rehome/release).

Desktop app only.

### `corsia artifact`

### `corsia artifact reconcile`

Preview or apply known artifact pane repairs for a thread.

| Argument | Description |
|---|---|
| `--as-session` | Thread id the CLI acts as (the actor/principal for the audited verb).; Required |
| `--thread-id` | — |
| `--mode` | Choices: preview, apply |

### `corsia artifact register`

Register an artifact to a thread.

| Argument | Description |
|---|---|
| `--as-session` | Thread id the CLI acts as (the actor/principal for the audited verb).; Required |
| `--integration` | — |
| `--type` | — |
| `--external-id` | — |
| `--path` | — |
| `--parent-thread-id` | — |
| `--title` | — |
| `--description` | — |
| `--retention` | Choices: ephemeral, durable |
| `--metadata` | JSON object |

### `corsia artifact rehome`

Transfer artifact ownership (audited).

| Argument | Description |
|---|---|
| `--as-session` | Thread id the CLI acts as (the actor/principal for the audited verb).; Required |
| `--integration` | Required |
| `--type` | Required |
| `--external-id` | Required |
| `--new-owner-thread-id` | Required |
| `--reason` | Required |
| `--authority` | — |
| `--metadata` | JSON object |

### `corsia artifact release`

Release artifact ownership (audited de-own).

| Argument | Description |
|---|---|
| `--as-session` | Thread id the CLI acts as (the actor/principal for the audited verb).; Required |
| `--integration` | Required |
| `--type` | Required |
| `--external-id` | Required |
| `--reason` | Required |
| `--authority` | — |
| `--triage-thread-id` | — |
| `--metadata` | JSON object |

### `corsia artifact relocate`

Move an artifact's backing file, preserving identity.

| Argument | Description |
|---|---|
| `--as-session` | Thread id the CLI acts as (the actor/principal for the audited verb).; Required |
| `--artifact-id` | Required |
| `--target-path` | Required |

### `corsia artifact resolve`

Resolve an artifact by id, path, or integration/type/external_id.

| Argument | Description |
|---|---|
| `--as-session` | Thread id the CLI acts as (the actor/principal for the audited verb).; Required |
| `--artifact-id` | — |
| `--path` | — |
| `--integration` | — |
| `--type` | — |
| `--external-id` | — |

### `corsia artifact update`

Update an artifact description or pane visibility.

| Argument | Description |
|---|---|
| `--as-session` | Thread id the CLI acts as (the actor/principal for the audited verb).; Required |
| `--artifact-id` | Required |
| `--description` | — |
| `--hide` | — |
| `--unhide` | — |
| `--thread-id` | — |

## `bind-host`

Print the interface CORSIA listeners bind

Desktop app only.

### `corsia bind-host`

| Argument | Description |
|---|---|
| `--bootstrap` | Bootstrap file (default: ~/.corsia/config.json) |

## `context`

CTX bootloader: attach\|create

Routed in source installs and the desktop app.

### `corsia context`

Manage workspace, project, and thread CTX bootloader files.

| Argument | Description |
|---|---|
| `--token` | API token (default: ~/.corsia/credentials/api-token) |
| `--base` | API base URL (default: http://127.0.0.1:1956) |

### `corsia context attach`

Attach an existing file to a bootloader

| Argument | Description |
|---|---|
| `scope` | Choices: workspace, global, project, thread; Required |
| `target_id` | project id or thread id; ignored for workspace/global; Required |
| `path` | Required |

### `corsia context create`

Create and attach a bootloader file

| Argument | Description |
|---|---|
| `scope` | Choices: workspace, global, project, thread; Required |
| `target_id` | project id or thread id; ignored for workspace/global; Required |
| `path` | Required |
| `--content` | — |

## `delegate`

Delegation: create-thread\|task\|send\|set-model\|status

Routed in source installs and the desktop app.

### `corsia delegate`

Coordinator delegation — create threads, send turns, read status.

| Argument | Description |
|---|---|
| `--token` | API token (default: ~/.corsia/credentials/api-token) |
| `--base` | API base URL (default: http://127.0.0.1:1956) |

### `corsia delegate create-thread`

Create a new delegated thread

| Argument | Description |
|---|---|
| `--title` | — |
| `--project-id` | — |
| `--model` | Required explicit model id; Required |
| `--provider` | — |
| `--thread-kind` | Thread visibility kind: chat is visible; subagent is a hidden child and requires a parent; Choices: chat, subagent; Default: chat |
| `--spawned-by` | Parent thread id for a hidden subagent |
| `--from-thread-id` | Your own thread id (default: $CORSIA_THREAD_ID): a subagent's parent, and the creator for any kind |

### `corsia delegate send`

Send a turn into a thread

| Argument | Description |
|---|---|
| `thread_id` | Required |
| `message (+)` | The message text; Required |
| `--from-thread-id` | Sender thread id (default: $CORSIA_THREAD_ID when present) |
| `--target-scope-id` | Required for intentional cross-scope sends; must match the target thread's project id, or 'workspace' for workspace threads |

### `corsia delegate set-model`

Switch a thread's selected model (resume-recovery for #447)

| Argument | Description |
|---|---|
| `thread_id` | Required |
| `model` | Explicit target dispatch form (e.g. claude-code/opus, anthropic/<id>, codex/...); Required |
| `--reason` | Audit reason (default: resume-recovery) |
| `--from-thread-id` | Sender thread id (default: $CORSIA_THREAD_ID when present) |

### `corsia delegate status`

Read a thread's turn-state + recent output

| Argument | Description |
|---|---|
| `thread_id` | Required |
| `--messages` | Recent messages to include (default 5, max 50); Default: 5 |

### `corsia delegate task`

Create a hidden child thread and send its first task turn

| Argument | Description |
|---|---|
| `message (+)` | The task text; Required |
| `--title` | Child thread title |
| `--model` | Required explicit model id; Required |
| `--provider` | — |
| `--from-thread-id` | Parent/sender thread id (default: $CORSIA_THREAD_ID when present) |

## `doc`

Document collaboration commands

Routed in source installs and the desktop app.

### `corsia doc`

Agent doc-collab — comment / reply / review / resolve / ack / read.

| Argument | Description |
|---|---|
| `--token` | API token (default: ~/.corsia/credentials/api-token) |
| `--base` | API base URL (default: http://127.0.0.1:1956) |

### `corsia doc accept`

Accept a document suggestion

| Argument | Description |
|---|---|
| `comment_id` | Required |
| `--all-from-author` | Default: False |
| `--as` | Authoring thread id (default: $CORSIA_THREAD_ID) |

### `corsia doc ack`

Acknowledge a comment (agree; ends the exchange)

| Argument | Description |
|---|---|
| `comment_id` | Required |
| `--as` | Authoring thread id (default: $CORSIA_THREAD_ID) |

### `corsia doc comment`

Anchored comment on a doc span

| Argument | Description |
|---|---|
| `target` | Doc path (or an artifact id with --id); Required |
| `body` | Comment body — one arg, e.g. "$(cat msg.md)"; Required |
| `--quote` | The span text to anchor to; Required |
| `--near` | Disambiguating hint for a repeated span |
| `--by-id` | Treat <target> as an artifact id, not a path; Default: False |
| `--as` | Authoring thread id (default: $CORSIA_THREAD_ID) |

### `corsia doc comments`

List a doc's comment threads (read)

| Argument | Description |
|---|---|
| `path` | Doc path; Required |
| `--unresolved` | Only unresolved threads; Default: False |
| `--json` | Raw JSON output; Default: False |
| `--as` | Authoring thread id (default: $CORSIA_THREAD_ID) |

### `corsia doc reject`

Reject a document suggestion

| Argument | Description |
|---|---|
| `comment_id` | Required |
| `--all-from-author` | Default: False |
| `--as` | Authoring thread id (default: $CORSIA_THREAD_ID) |

### `corsia doc reply`

Reply to a comment thread

| Argument | Description |
|---|---|
| `comment_id` | The parent comment id; Required |
| `body` | Reply body — one arg; Required |
| `--path` | Doc path (optional; scopes the reply) |
| `--id` | Artifact id (optional; scopes the reply) |
| `--as` | Authoring thread id (default: $CORSIA_THREAD_ID) |

### `corsia doc request-review`

Queue a review task in another agent's plan

| Argument | Description |
|---|---|
| `target` | Doc path (or an artifact id with --by-id); Required |
| `--reviewer` | Reviewer thread ID from agent_roster; Required |
| `--note` | Optional requester's note (at most 500 characters) |
| `--client-id` | Stable request key for retries |
| `--by-id` | Default: False |
| `--as` | Authoring thread id (default: $CORSIA_THREAD_ID) |

### `corsia doc resolve`

Resolve (close) a comment thread

| Argument | Description |
|---|---|
| `comment_id` | Required |
| `--as` | Authoring thread id (default: $CORSIA_THREAD_ID) |

### `corsia doc retry`

Retry feedback batches on a document

| Argument | Description |
|---|---|
| `target` | Doc path (or an artifact id with --id); Required |
| `--by-id` | Default: False |
| `--as` | Authoring thread id (default: $CORSIA_THREAD_ID) |

### `corsia doc review`

Whole-doc review comment

| Argument | Description |
|---|---|
| `target` | Doc path (or an artifact id with --id); Required |
| `body` | Review body — one arg; Required |
| `--by-id` | Treat <target> as an artifact id, not a path; Default: False |
| `--as` | Authoring thread id (default: $CORSIA_THREAD_ID) |

### `corsia doc review-cancel`

Cancel a document review request

| Argument | Description |
|---|---|
| `request_id` | Required |
| `--as` | Authoring thread id (default: $CORSIA_THREAD_ID) |

### `corsia doc submit`

Submit feedback batches on a document

| Argument | Description |
|---|---|
| `target` | Doc path (or an artifact id with --id); Required |
| `--by-id` | Default: False |
| `--as` | Authoring thread id (default: $CORSIA_THREAD_ID) |

### `corsia doc suggest`

Suggest an anchored replacement

| Argument | Description |
|---|---|
| `target` | Doc path (or an artifact id with --id); Required |
| `quote` | Exact current text to replace; Required |
| `replacement` | Replacement text; empty deletes the quote; Required |
| `body (?)` | Optional note; Default:  |
| `--near` | Disambiguating hint for a repeated span |
| `--by-id` | Default: False |
| `--as` | Authoring thread id (default: $CORSIA_THREAD_ID) |

## `event`

Emit a declared event from a local program.

Desktop app only.

### `corsia event`

### `corsia event emit`

Emit one declared event through the live intake.

| Argument | Description |
|---|---|
| `event_type` | Declared event type, such as qa.review_completed.; Required |
| `--facts` | JSON object, or - to read one JSON object from stdin.; Required |
| `--id` | Stable identity for a retry-safe event. |
| `--as-session` | Chat id the event is emitted as (default: $CORSIA_THREAD_ID). |

## `init`

First-run setup — configure workspace path

Desktop app only.

### `corsia init`

| Argument | Description |
|---|---|
| `--workspace, --path` | Workspace path (skips the prompt) |
| `--name` | Human-readable workspace name |
| `--setup` | local or server (skips the prompt); Choices: local, server |
| `--force` | Overwrite existing config; Default: False |

## `local`

Set up a local model

Routed in source installs and the desktop app.

### `corsia local`

### `corsia local setup`

Install oMLX and the local model its recommender picks for this Mac

| Argument | Description |
|---|---|
| `--if-no-subscription` | Skip automatic setup when Claude Code or Codex is already signed in; Default: False |
| `--automatic` | Default: False |
| `--start-only` | Default: False |
| `--before-start` | Default: False |

## `logs`

Tail the backend log

Routed in source installs and the desktop app.

`corsia logs [backend]`

## `mcp`

Configure external MCP tools

Desktop app only.

### `corsia mcp`

### `corsia mcp add`

Add a connected MCP server

| Argument | Description |
|---|---|
| `--name` | Required |
| `--config` | JSON config object; Required |
| `--key-refs` | JSON list of key references; Required |
| `--reach` | JSON reach object; the server authorizes its use |

### `corsia mcp approve`

Approve a pending MCP server change

| Argument | Description |
|---|---|
| `server_id` | Required |
| `--expected-revision` | Required |

### `corsia mcp configure`

Retired; add apps through Connected apps

| Argument | Description |
|---|---|
| `file` | Required |

### `corsia mcp edit`

Edit a connected MCP server

| Argument | Description |
|---|---|
| `server_id` | Required |
| `--config` | JSON config object; Required |
| `--key-refs` | JSON list of key references; Required |
| `--expected-revision` | Required |

### `corsia mcp list`

List connected MCP servers

| Argument | Description |
|---|---|
| `--project` | List server reach for this project |
| `--workspace` | List workspace-wide servers; Default: False |

### `corsia mcp reconnect`

Ask the server to reconnect an MCP app

| Argument | Description |
|---|---|
| `server_id` | Required |

### `corsia mcp remove`

Remove a connected MCP server

| Argument | Description |
|---|---|
| `server_id` | Required |

## `open`

Open CORSIA

Routed in source installs and the desktop app.

`corsia open [--force]`

## `perf`

Historical latency reports: routes\|tools

Routed in source installs and the desktop app.

### `corsia perf`

### `corsia perf routes`

Historical route latency; needs the running server

| Argument | Description |
|---|---|
| `--days` | Inclusive UTC day window: today plus the previous N-1 days (1-90; default: 7); Default: 7 |
| `--route` | Normalized route-template glob, for example /api/threads/* |

### `corsia perf tools`

Historical aggregate tool and LLM latency

| Argument | Description |
|---|---|
| `--days` | Inclusive UTC day window: today plus the previous N-1 days (1-90; default: 7); Default: 7 |

## `providers`

Providers and models: list\|models\|add\|edit\|remove\|test\|window\|...

Routed in source installs and the desktop app.

### `corsia providers`

corsia providers — CLI shim over the provider and model-catalog APIs.

| Argument | Description |
|---|---|
| `--base` | API base URL (default $CORSIA_API_BASE or local) |
| `--json` | print JSON instead of text; Default: False |

### `corsia providers add`

register an External endpoint (add the key in Providers)

| Argument | Description |
|---|---|
| `--name` | Required |
| `--base-url` | Required |
| `--dialect` | Choices: openai, anthropic; Default: openai |
| `--model` | a model id it serves, if it cannot list them (repeatable) |

### `corsia providers disable`

switch an endpoint off

| Argument | Description |
|---|---|
| `endpoint` | Required |

### `corsia providers edit`

change an External endpoint; omitted fields are kept (add the key in Providers)

| Argument | Description |
|---|---|
| `endpoint` | Required |
| `--base-url` | — |
| `--dialect` | Choices: openai, anthropic |
| `--model` | replace the declared model ids (repeatable) |
| `--clear-models` | drop the declared model ids; Default: False |

### `corsia providers enable`

switch an endpoint on

| Argument | Description |
|---|---|
| `endpoint` | Required |

### `corsia providers hide`

take a model out of the picker

| Argument | Description |
|---|---|
| `model` | Required |

### `corsia providers list`

providers and endpoints, with status

### `corsia providers models`

catalog rows

| Argument | Description |
|---|---|
| `--provider` | filter by provider, harness or endpoint id |
| `--all` | include hidden models; Default: False |

### `corsia providers remove`

remove an endpoint

| Argument | Description |
|---|---|
| `endpoint` | Required |

### `corsia providers rename`

set a model's display label

| Argument | Description |
|---|---|
| `model` | Required |
| `label` | Required |

### `corsia providers show`

put a model in the picker

| Argument | Description |
|---|---|
| `model` | Required |

### `corsia providers test`

probe an endpoint now

| Argument | Description |
|---|---|
| `endpoint` | Required |

### `corsia providers window`

set a model's context window

| Argument | Description |
|---|---|
| `model` | Required |
| `tokens` | a whole number of tokens, or 'clear'; Required |

## `remote-address`

Print the saved outside address while devices can connect

Desktop app only.

### `corsia remote-address`

| Argument | Description |
|---|---|
| `--bootstrap` | Bootstrap file (default: ~/.corsia/config.json) |
| `--state-root` | State directory to read instead of the bootstrap's (the app's own) |

## `restart`

Restart CORSIA

Routed in source installs and the desktop app.

`corsia restart [--force]`

## `service`

Manage the app service

Routed in source installs and the desktop app.

`corsia service [install|uninstall|start|stop|restart|status]`

`install` and `uninstall` are available in source installs; the desktop app refuses them.

## `setup`

Choose local or server exposure

Routed in source installs and the desktop app.

### `corsia setup`

| Argument | Description |
|---|---|
| `setup` | Choices: local, server; Required |

In source installs, `corsia setup local|server` updates the choice and restarts the service. The desktop app routes this command directly to the Python CLI.

## `sources`

Which model sources are ready, one fix each

Routed in source installs and the desktop app.

### `corsia sources`

| Argument | Description |
|---|---|
| `action (?)` | Choices: recheck |
| `--install-summary` | Default: False |

## `start`

Start CORSIA

Routed in source installs and the desktop app.

`corsia start [--open] [--force]`

## `status`

Show CORSIA status

Routed in source installs and the desktop app.

## `stop`

Stop CORSIA

Routed in source installs and the desktop app.

`corsia stop [--stop-local-model] [--force]`

## `token`

API tokens: create\|list\|revoke\|service

Routed in source installs and the desktop app.

### `corsia token`

### `corsia token create`

Mint an API token (shown once)

| Argument | Description |
|---|---|
| `--user` | Account (default: the only one) |
| `--label` | Label to identify the token later |
| `--write-delegate-file` | Write it to the agent credential file instead of printing it; Default: False |

### `corsia token list`

List tokens, never their values

| Argument | Description |
|---|---|
| `--user` | Account (default: every account) |

### `corsia token revoke`

Revoke tokens by label

| Argument | Description |
|---|---|
| `label` | Label to revoke; Required |
| `--user` | Account (default: the only one) |

### `corsia token service`

Mint the lifecycle shell's 1-hour service token

## `user`

Accounts: add\|passwd\|list

Routed in source installs and the desktop app.

### `corsia user`

### `corsia user add`

Create an account

| Argument | Description |
|---|---|
| `name (?)` | Username: 1-32 of a-z, 0-9, - or _ |
| `--password-stdin` | Read the password from stdin; Default: False |

### `corsia user list`

List accounts

### `corsia user passwd`

Change a password

| Argument | Description |
|---|---|
| `name` | Account to update; Required |
| `--password-stdin` | Read the password from stdin; Default: False |

## Internal commands

These commands are for internal operation.

- `discard-prepared`: Discard a prepared release
- `ensure-current-release`: Ensure the frontend release is current
- `prepare-redeploy`: Prepare a redeploy
- `redeploy`: Pull --ff-only + build + bounce THIS instance's serving checkout
- `restart-prepared`: Restart a prepared release
