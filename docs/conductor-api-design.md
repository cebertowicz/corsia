# Conductor API and instruction setup

The CORSIA agent and designated project conductors can call application handlers through a bounded, credential-free tool, except routes on its person-only blocklist.

## Code ground

The canonical HTTP owner remains `main.app` and its registered FastAPI handlers. `corsia_api` invokes that ASGI application in process; it does not duplicate services or create a network login. Existing handler ownership checks still use the actor's user ID. A revocable task-local principal carries the originating thread ID and stops authenticating inherited background contexts when the request exits.

The route policy uses a method and path blocklist. Registered routes are open unless they match it; this includes newly registered routes. The blocklist covers authentication, credentials, host-access authority, recursive tool bridges, and specified handlers that record a human actor. Request fields cannot claim another sender, creator, or document author. Recognizable credential fields and values are rejected on input and sanitized on output. Private-file inputs preserve the existing host-path safety floor (`backend/tools/conductor_api_policy.py`, `backend/tools/conductor_api_identity.py`, `backend/tools/conductor_api_paths.py`).

Agent creation and promotion use `delegate_create_thread` and revisioned `agent_roster_update`. Instruction creation uses the guidance saver, bootloader attachment transaction, skills registry and lesson eligibility partition (`backend/tools/conductor_instruction_create.py`). It installs thread responsibility files or skills with support files, and reports resolution-set inclusion for the next turn. A fresh target turn remains the final delivery check.

## State and failure behavior

An instruction bundle requires `absent` or its returned revision; identical desired bytes are an idempotent retry. Revisions include destination paths so same-named script and reference files remain distinct. Saved bytes are checked before attachment. A stale edit refuses replacement; an interrupted API write requires reading the destination before retry.

Requests and collected responses are limited to 32 KiB, and each request has a 30-second deadline. A handler may complete a write before a timeout or response-limit error. Check the destination before retrying a write; the timeout error says to do so (`backend/tools/corsia_api.py`).

## Agent journey

| Step | Surface and acceptance |
| --- | --- |
| Discover | System skills index includes `coordination-api`; tool descriptions direct setup to it. |
| Inspect | The CORSIA agent reads its roster through native dispatch and the real API handler. |
| Create | Existing delegation creates a visible thread using a catalog-valid model. |
| Promote | Revisioned roster update enables the agent and supplies its responsibility. |
| Attach | Instruction tool saves responsibility, attaches it, and reports next-turn inclusion. |
| Verify | Roster read contains the new agent; repeated desired state changes nothing. |
| Recover | Threads without conductor capability receive an explicit API capability denial; stale edits require current files and revision. |

## Verification surface

`backend/tests/test_conductor_setup_journey.py` exercises discovery, creation, promotion, instruction attachment, roster reads and a denied route through native `ToolRegistry` dispatch, with disposable state and mock delegation. `backend/tests/test_corsia_api_tool.py` and `backend/tests/test_conductor_instruction_create.py` cover the API and instruction tools.

The tests use disposable state and mock delegation; they do not prove delivery by a serving instance.

## Complexity ledger

### Adds

The broker module, route manifest, origin binding, path guard and redaction.

### Removes or collapses

The broker invokes existing handlers (`backend/tools/corsia_api.py`).

### Ledger

The broker sends requests to the application's registered handlers (`backend/tools/corsia_api.py`).

### Falsification / removal condition

The broker uses a revocable task-local principal and binds attribution to the acting thread (`backend/tools/corsia_api.py`, `backend/infra/internal_principal.py`).

Checked against the code at `7a8c55d84`.
