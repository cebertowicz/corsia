# Conductor API and instruction setup

This additive capability gives the designated workspace conductor bounded, credential-free access to reviewed application handlers. Project conductors use existing scoped roster and instruction tools. Human-only operations retain their existing entry points.

## Code ground

The canonical HTTP owner remains `main.app` and its registered FastAPI handlers. `corsia_api` invokes that ASGI application in process; it does not duplicate services or create a network login. Existing handler ownership checks still use the actor's user ID. A revocable task-local principal carries the originating thread ID and stops authenticating inherited background contexts when the request exits.

The route policy contains permanent denied families plus a reviewed method/path inventory. Authentication, credentials, host-access authority, recursive tool bridges, control-plane restart/drain, and handlers that explicitly record a human actor are denied. New routes remain denied until classified. Request bodies cannot claim another sender, creator, or document author. Recognizable credential fields and values are rejected on input and sanitized on output. Private-file inputs preserve the existing host-path safety floor.

Agent creation and promotion reuse `delegate_create_thread`, the existing agent service, and revisioned `agent_roster_update`. No second creation service or roster state owner is introduced. Instruction creation uses the existing guidance saver, bootloader attachment transaction, skills registry and lesson eligibility partition. It installs thread responsibility files or skills with support files, and reports resolution-set inclusion for the next turn. A fresh target turn remains the final delivery check.

## State and failure behavior

No schema migration is needed. Existing threads, roster state and attached files remain intact. An instruction bundle requires `absent` or its returned revision; identical desired bytes are an idempotent retry. Revisions include destination paths so same-named script and reference files remain distinct. Saved bytes are checked before attachment. A stale edit refuses replacement; an interrupted API write requires reading the destination before retry.

Requests and collected responses are limited to 32 KiB, and each request has a 30-second deadline. A handler may complete a write before a timeout or response-limit error. These errors therefore instruct the caller to verify destination state. No background loop, polling service or provider inference call is introduced by the broker.

## Agent journey

| Step | Surface and acceptance |
| --- | --- |
| Discover | System skills index includes `conductor-api`; tool descriptions direct setup to it. |
| Inspect | Workspace conductor reads its roster through native dispatch and the real API handler. |
| Create | Existing delegation creates a visible thread using a catalog-valid model. |
| Promote | Revisioned roster update enables the agent and supplies its approved responsibility. |
| Attach | Instruction tool saves responsibility, attaches it, and reports next-turn inclusion. |
| Verify | Roster read contains the new agent; repeated desired state changes nothing. |
| Recover | Ordinary/project actors receive explicit API capability denial; stale edits require current files and revision. |

## Verification surface

`backend/tests/test_conductor_setup_journey.py` exercises the composed journey through native `ToolRegistry` dispatch, with disposable state and mock delegation. Other targeted tests cover route enumeration, every denied route, conductor eligibility, cross-scope installation refusal, inherited-principal revocation, request/response bounds, credential redaction, origin forgery, stale revisions, support-file validation and next-turn skill inclusion. Authentication, agent service, delegation and instruction-file regression suites cover affected consumers.

A fresh inexpensive model reader independently checked the positive setup case, the neighboring read-only roster case and denial/stale-state recovery without a correction round. This supplements mock dispatch and resolver evidence; it does not claim actual delivery of proposed code by a serving instance.

The independent security review's demonstrated origin-forgery and standalone-key disclosure counterexamples are covered by broker regression tests. Same-user workspace-wide administration remains intentional. Concurrent local symlink replacement during instruction saving is a residual filesystem race hypothesis; initial traversal and symlink escapes are rejected. No real inference or daemon action is executed by these tests.

## Complexity ledger

### Adds

The broker module, route manifest, origin binding, path guard and redaction.

### Removes or collapses

None. No per-feature conductor tools are replaced by this change.

### Ledger

Reusing the application's existing handlers keeps validation, authorization, audit, events and state ownership in one path. A second handler layer would duplicate those contracts and create another place for them to diverge.

### Falsification / removal condition

When #1182 lands, agent calls authenticate as a scoped agent principal, and `bind_delegation_origin`, `attribution_matches` and the credential regex layers are deleted.
