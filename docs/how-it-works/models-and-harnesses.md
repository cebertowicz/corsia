# Models and harnesses

A model is the language model a thread talks to. A harness is the program that runs the agent loop around it. This page covers how a model is chosen, how each harness runs, what a model switch preserves, and what can go wrong.

## What it is

- **Model:** one row in the model catalog, with a provider and capability information (such as reasoning and vision). A context window can be unknown.
- **Harness:** one of `native`, `claude-code` or `codex` (`backend/llm/resolution.py`). It runs the loop: sends the prompt, receives tool calls, runs them, repeats until the turn ends.
- **Execution target:** the resolved, immutable pairing of a model, its harness, its provider and its window, carried through a turn's dispatch.
- **Account:** a login for a vendor harness. A project can restrict which accounts its threads may use.

Two choices stay separate. The harness decides who runs the loop. For the native harness, the inference provider decides who serves the model: a cloud provider, a server on the local network, or a locally managed server.

## Why it exists

People have different subscriptions, hardware and tolerance for sending data off their machine. The platform does not fix any one of these. It keeps conversation history, plans and context in its own store, so a thread can change model or harness without losing its record. The harness differences are contained in one dispatch layer instead of spreading through the codebase.

## How it works

### The catalog

The catalog is built from discovery of cloud providers, servers on the local network, a locally managed oMLX server, and the Claude Code and Codex harnesses (`backend/llm/catalog.py`, `backend/llm/catalog_cloud.py`, `backend/llm/catalog_network_models.py`, `backend/llm/harness_catalog.py`). It is served at `/api/models`. Each row says whether the model is available now.

**Context window.** The window used for checkpointing is resolved afresh. When known, it is the provider's window capped by 262,144 tokens unless the model id names a larger one (such as an explicit `[1m]` suffix) or an operator has saved an override (`backend/llm/context_window.py`).

### No silent fallback

If the model a thread selected cannot run, the turn ends with a named reason. A temporarily unavailable selected model can leave its input queued until the catalog reports it runnable again; other resolution failures fail the input (`backend/infra/unavailable_model_recovery.py`, `backend/llm/resolution.py`). CORSIA does not substitute another model. A model the person did not choose has different cost, privacy and capability, and substituting it would hide that.

### The three harnesses

| Harness | How a turn runs |
|---|---|
| `native` | CORSIA's own loop, calling models through LiteLLM (`backend/llm/router.py`). Up to 200 tool iterations per turn by default (`backend/core/agent.py`, `backend/core/turn_iteration.py`). |
| `claude-code` | The `claude` program in print mode with streaming JSON in and out. An ordinary managed launch uses CORSIA's tool bridge when configured and switches off Claude Code's auto memory, project instruction file, hooks and bundled skills; a confined launch instead uses its read-only native tools (`backend/subagents/cc_invocation.py`, `backend/subagents/managed_launch.py`). |
| `codex` | `codex app-server` over standard I/O, with provider-native actions, skills, search and project docs disabled. It starts with `--strict-config`, so a Codex release that removes or renames a governed control makes startup fail visibly instead of silently restoring a native tool. A turn starts with `turn/start`, and later input joins the running turn with `turn/steer` (`backend/subagents/codex_launch_policy.py`, `backend/subagents/managed_launch.py`, `backend/subagents/codex_runtime_support.py`). |

The dispatch layer maps execution classes to the three harnesses and their handlers (`backend/infra/send_dispatch.py`). Input delivery also uses harness-specific safe-boundary handling (`backend/infra/send_service.py`).

A worker created with `untrusted` confinement is limited to file reads; see [Security](../security/README.md).

### Local models

CORSIA can start and supervise its own oMLX server for local inference, probe it, and resume a pending setup or download at startup (`backend/llm/providers/omlx_supervisor.py`, `backend/llm/local_setup.py`). Local steps that fail at startup are logged and do not stop the app. A network server is also supported (`backend/llm/network_endpoints.py`).

### Switching models

A model switch keeps the thread and its history. How the destination starts depends on the case (`backend/thread/model_row.py`):

| Mode | The destination starts from |
|---|---|
| `exact` | CORSIA's persisted history, in full |
| `checkpoint_tail` | One installed checkpoint plus the exact recent history |
| `provider_resume` | The same harness conversation, continued with the new model; CORSIA does not rebuild history |

When the destination needs a new checkpoint, the pending switch can start one before committing the change (`backend/thread/model_pending.py`). A switch is also a context install boundary; see [Context and memory](context-and-memory.md).

### Account rollover

When an account reaches a usage limit, or a project's policy no longer permits it, a thread can move to another account if rollover is enabled. The destination must run the same model, be ready, and report usage as allowed; unknown usage is not enough (`backend/connections/account_rollover.py`). A move happens before the next queued input is claimed.

## What can go wrong

- **A model disappears.** If a provider stops listing a model or a harness is not signed in, work cannot run on that binding and the reason is visible. Some unavailable models hold their input for recovery; nothing falls back.
- **Harness behavior differs.** The same task can take a different number of steps, and the harnesses accept mid-turn input differently. See [Turns and delivery](turns-and-delivery.md).
- **A small window means frequent checkpoints.** See [Context and memory](context-and-memory.md).
- **A harness program changes.** Claude Code and Codex are separate programs run with specific flags. A release of either that changes those flags or protocols can break its harness until CORSIA is updated. The Codex launch overrides do not isolate arbitrary user or project Codex configuration; the code says so in its own comments.
- **Local setup can be interrupted.** Startup resumes it, and failures are recorded with a reference rather than blocking the app.

## Where it lives

| Part | Path |
|---|---|
| Resolution, no-fallback error | `backend/llm/resolution.py` |
| Catalog and discovery | `backend/llm/catalog.py`, `backend/llm/harness_catalog.py` |
| Context window rule | `backend/llm/context_window.py` |
| Native loop and router | `backend/core/agent.py`, `backend/llm/router.py` |
| Claude Code harness | `backend/subagents/cc_invocation.py`, `backend/subagents/managed_launch.py` |
| Codex harness | `backend/subagents/codex_launch_policy.py`, `backend/subagents/codex_runtime_support.py` |
| Dispatch table | `backend/infra/send_dispatch.py` |
| oMLX supervision | `backend/llm/providers/omlx_supervisor.py` |
| Model switching | `backend/thread/model_row.py`, `backend/thread/model_state.py` |
| Account rollover | `backend/connections/account_rollover.py` |
