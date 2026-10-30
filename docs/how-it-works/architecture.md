# Architecture

CORSIA has a backend process that manages application state, a browser UI it serves, and a set of agent loops it starts and supervises. This page covers the processes, where state lives, what happens at startup, and what can go wrong.

## What it is

- **One backend.** A FastAPI application serves REST routes, one WebSocket (`/ws`) for live chat, a browser-pane stream, and the built UI. It manages the application's database writes.
- **One UI.** A SvelteKit app built as a static site into the `dist` folder of `frontend/`. The backend serves those files and, when `index.html` exists, falls back to it for non-file paths, so the UI is client-routed. Without a build it returns a JSON status; a missing index returns 404. An unmatched GET under `/api/` returns a JSON 404, not the UI page (`backend/main.py`).
- **A desktop shell, optional.** `desktop/src-tauri` is a Tauri shell that starts or attaches to a local service, shows startup progress, then opens the local UI. `desktop/launchd` holds the service definition for the packaged app. The backend also has its own entry point (`backend/main.py`), so the UI works from a browser without the shell.
- **Agent loops.** Each turn runs in one of three harnesses: CORSIA's native loop, Claude Code, or Codex. See [Models and harnesses](models-and-harnesses.md).

```mermaid
flowchart TB
    UI[Browser or<br/>desktop shell] -->|REST, /ws| BE[Backend<br/>FastAPI]
    BE --> DB[(SQLite<br/>corsia.db)]
    BE --> AN[(DuckDB<br/>analytics)]
    BE --> WS[Workspace<br/>files]
    BE -->|native loop| LLM[Model providers]
    BE -->|spawns| CC[Claude Code]
    BE -->|spawns| CX[Codex]
    CC -->|MCP bridge when configured| BE
    CX -->|tools| BE
```

## Why it is shaped this way

- **Central input admission.** The backend admits inputs from a user, another agent or an external event, so it can order them and recover queued work after a crash. Delivery guarantees in [Turns and delivery](turns-and-delivery.md) depend on this.
- **Files for context, database for state.** Instructions, skills and shared documents are plain files in the workspace, so a person and an agent edit them the same way and version them with Git. Conversations, plans, routing and settings are rows, because they need transactions.
- **Agents reach CORSIA through tools.** Managed Claude Code and Codex sessions can call into the backend through its tool interface, so a plan update or a delegation goes through the same code whichever harness made it. A confined Claude Code worker uses read-only native tools instead.

## How it works

### Where state lives

- **Workspace folder:** context files, skills, shared documents and project folders. See [Scopes](scopes.md).
- **State root:** `corsia.db`, `analytics.duckdb` and `uploads/`. A separate `otel_logs.db` holds logs. `CORSIA_STATE_DIR` can set the root; otherwise the bootstrap's root or a platform-specific default is used. Legacy bootstraps retain their workspace-local state, and incompatible overrides are refused. On macOS the default differs between a source install and the packaged app (`backend/infra/config.py`, `backend/infra/platform_paths.py`).
- **Database:** SQLite in WAL mode, with a busy timeout and `synchronous=NORMAL` (`backend/infra/database.py`).
- **Analytics:** DuckDB, optional. If the package is missing, analytics turn off and the backend starts anyway (`backend/infra/product_events.py`).

### Schema changes

The schema version is stored in the database (`PRAGMA user_version`) and compared with the code's version (`backend/infra/schema.py`) at every start.

- An empty database with version zero or the current version gets the current schema. An empty database marked with another version is refused.
- An older database is migrated forward in place by registered steps (`backend/infra/database_migrations.py`).
- A database with no registered path to the current version, or written by a newer build, is refused with a named error rather than guessed at.

### Startup order

Startup runs in two stages, in the application's lifespan hook (`backend/app_lifecycle.py`).

1. **Synchronous init** (`backend/app_startup_init.py`): create runtime folders, initialize or migrate the database, repair moved paths, sweep leftover private launch files and key-store values, then recover interrupted durable writes and file relocations and initialize the workspace folders.
2. **Runtime start** (`backend/app_lifecycle.py`, `backend/app_startup_runtime.py`): reconcile in-flight turns from the previous run (see [Turns and delivery](turns-and-delivery.md)) before starting the runtime services, which include the background loops: plan liveness, presence sweep, GitHub notification polling, mail polling, skill-library refresh, owned-PR quiet watch, and nightly jobs.

Startup steps for local models are independent: each failure is logged with a reference and does not stop the app.

### Network exposure

The backend binds to one interface chosen at setup: `127.0.0.1` for "this computer only" or `0.0.0.0` for "reachable from other devices". An invalid or inconsistent setting fails closed with the command that fixes it. A missing bootstrap uses the legacy all-interface bind, and an older bootstrap without either setting is updated to retain that bind (`backend/infra/bind_host.py`). The default port is 1956 (`backend/infra/default_port.py`).

## What can go wrong

- **Startup refusal.** A database from a newer build is refused. Downgrading CORSIA over a newer state root does not work.
- **Single process.** One backend means one place to fail. Turns running when it dies are ended with a named reason on the next start; they are not resumed mid-turn.
- **Optional pieces can be unavailable.** Missing DuckDB turns off analytics and is logged; a failed local-model resume is also logged. Check the logs if a feature seems absent.
- **Platform.** The project README states the supported platform. The repository also contains a Linux sandbox path (`docs/linux-sandbox.md`, `scripts/install-linux-sandbox.sh`); read both before assuming either way.

## Where it lives

| Part | Path |
|---|---|
| Server entry, routes, static UI serving | `backend/main.py` |
| Startup and lifecycle | `backend/app_lifecycle.py`, `backend/app_startup_init.py`, `backend/app_startup_runtime.py` |
| Database, schema, migrations | `backend/infra/database.py`, `backend/infra/schema.py`, `backend/infra/database_migrations.py` |
| Live chat over WebSocket | `backend/ws/` |
| UI | `frontend/` (its `dist` build folder is what the backend serves) |
| Desktop shell | `desktop/src-tauri/` |
| Code map | [ARCHITECTURE.md](../../ARCHITECTURE.md), [BACKEND.md](../../BACKEND.md), [FRONTEND.md](../../FRONTEND.md) |

## Check it yourself

The schema version and the migration entry point:

```sh
grep -n "SCHEMA_VERSION" backend/infra/schema.py
grep -n "def initialize_schema" backend/infra/database.py
```
