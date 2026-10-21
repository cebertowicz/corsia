# Observability — Product Events

> Reference taxonomy for CORSIA's product-analytics emits (code and tests are implementation truth per AGENTS.md). Originally
> Sprint 4 Slice 3 of the 00-quality-foundation initiative. Sprint 5
> Slice 0 reversed the storage substrate from PostHog → DuckDB
> (DECISIONS-LOG #26); this document tracks the post-reversal state.

## Overview

Product events are user-action-scoped emits written to DuckDB at
`~/.corsia/analytics.duckdb` (Sprint 5 Slice 0; backend wrapper).
Frontend can emit to a separately configured PostHog endpoint. This
repository does not provide a local PostHog stack. Any relay replacement
requires a current plan/PRD; the archived subsystem spec does not authorize
future work. The schema is portable —
the same taxonomy works against DuckDB, ClickHouse, BigQuery, or
PostHog Cloud via `INSERT INTO target SELECT * FROM source`. They power:

- Evals tab live cards (Slice 8)
- Ad-hoc analytics via `tools/qh-query` CLI (DuckDB SQL helpers)

Emits are **not** the same as OTel spans. In-process evaluation tracing captures
traces and latency (Slice 2 / Slice 6). Product events capture user
intent and outcome at the seams. There is intentional overlap on some
events (e.g., `turn.started` has both an OTel span and a product
event) because the two surfaces answer different questions.

## Wrappers (the only public surface)

- **Backend:** `backend/infra/product_events.py` →
  `emit_product_event(category, action, *, session_id, user_id, properties)`
- **Frontend:** `frontend/src/lib/product_events.ts` →
  `emitProductEvent(category, action, properties)`
- **Dev tooling (CLI):** `tools/antipatterns/` uses a small
  `_add_backend_to_path` helper to import the backend
  wrapper, then call `emit_product_event` on tool exit. The fail-silent
  semantics from the backend wrapper apply — a broken analytics writer
  must never block a CI gate.

Callers MUST use these wrappers — no direct duckdb / posthog-js imports
outside `backend/infra/product_events.py` and
`frontend/src/lib/product_events.ts`. Why: configuration (env vars,
project tag), fail-silent behavior, and the schema shape are codified
once and exercised by tests.

## Event taxonomy

Event names are `<category>.<action>`. Lowercase, dot-separated, no spaces.

### Session lifecycle

| Event | When it fires | Surface |
|---|---|---|
| `session.started` | New conversation created (or first message into a fresh shell) | backend |
| `session.ended` | Conversation closed / archived | backend |
| `session.renamed` | Title updated (user or auto) | backend |
| `session.deleted` | Conversation hard-deleted | backend |

### Turn lifecycle

| Event | When it fires | Surface |
|---|---|---|
| `turn.started` | A new user→agent turn enters the dispatcher | backend |
| `turn.completed` | Turn produced a final assistant message | backend |
| `turn.cancelled` | User pressed stop / turn aborted | backend |
| `turn.errored` | Turn raised an unrecoverable error | backend |

### Model lifecycle

| Event | When it fires | Surface |
|---|---|---|
| `model.load.completed` | oMLX / lmstudio load fired and succeeded | backend |
| `model.load.failed` | Load attempt returned an error | backend |
| `model.unloaded` | Model unloaded | backend |
| `model.pin` / `model.unpin` | Per-model pin toggled on the daemon | backend |
| `model.switched` | Active model for a session changed | backend + frontend |

### Onboarding

All six onboarding events are backend-only lifecycle observations. They contain no user, project, thread, or model identifiers, decision text, or message content; the backend wrapper keeps them local and non-load-bearing.

| Event | When it fires | Surface |
|---|---|---|
| `onboarding.account_created` | The first account is created | backend |
| `onboarding.first_run_wake` | That account queues the CORSIA agent's first-run wake | backend |
| `onboarding.first_model_selected` | The CORSIA agent receives its first model | backend |
| `onboarding.first_turn_completed` | The CORSIA agent completes its first turn | backend |
| `onboarding.first_decision_answered` | The CORSIA agent's first decision is answered | backend |
| `onboarding.first_outcome` | The first accepted node close using `mark_done` or `accept` in a project created after the account | backend |

### Local setup

| Event | When it fires | Surface |
|---|---|---|
| `local_setup.started` | The backend starts the local starter-model setup flight | backend |
| `local_setup.skipped` | A setup request ends without a flight (`reason` ∈ `ineligible \| subscription_available`) | backend |
| `local_setup.failed` | The flight ends in failure (`error_type`: the exception class name only) | backend |
| `local_setup.completed` | The flight ends with the starter model ready | backend |

### Messages

| Event | When it fires | Surface |
|---|---|---|
| `message.sent` | User submitted from composer | frontend (primary), backend (verification) |
| `message.received` | Assistant message rendered to UI | frontend |
| `message.attached` | File/image added to a message | frontend + backend |

### Summary / consolidation

| Event | When it fires | Surface |
|---|---|---|
| `summary.fired` | Background summary job triggered | backend |
| `summary.completed` | Summary job finished | backend |

### Attachments

| Event | When it fires | Surface |
|---|---|---|
| `attachment.added` | Upload accepted | backend |
| `attachment.removed` | Upload removed pre-send | frontend |

### Cost accounting

| Event | When it fires | Surface |
|---|---|---|
| `cost.recorded` | Per-turn token/cost row written | backend |

### Plan dispatch

| Event | When it fires | Surface |
|---|---|---|
| `plan_dispatch.recorded` | A tool call starting under its executor's valid plan selection committed its dispatch row | backend |
| `plan_dispatch.unrecorded` | A tool call ran without a dispatch row: no Turn, or no valid selection | backend |
| `plan_dispatch.settled` | A recorded call ended and settled its own row | backend |
| `plan_dispatch.record_failed` | Recording raised; the call did not start | backend |
| `plan_dispatch.settle_failed` | Settling a started call's row raised | backend |
| `plan_dispatch.terminal_unknown` | A committed Turn terminal settled the recorded calls it left open as unknown | backend |

### Errors

| Event | When it fires | Surface |
|---|---|---|
| `error.api` | API handler returned 5xx | backend |
| `error.model` | Model call raised after retries | backend |
| `error.frontend` | Frontend hit a render or fetch error | frontend |

### Dev tooling

> The anti-pattern CLI emits on every invocation so analytics can compute runtime,
> block-rate, and finding-distribution KPIs from real signal. Each
> emit happens on tool exit (pass OR fail) — never blocks the tool's
> primary work even if analytics is down (the wrapper is contract-bound
> fail-silent; tool except wraps any leak past it).

| Event | When it fires | Surface |
|---|---|---|
| `dev_tool.antipatterns_invoked` | `tools/antipatterns/` CLI exits | CLI (pre-commit / CI / manual) |

### UI

| Event | When it fires | Surface |
|---|---|---|
| `ui.settings_opened` | Settings drawer opened | frontend |
| `ui.modal_opened` | Any modal (model details, image preview, etc.) | frontend |
| `ui.tab_switched` | Top-level tab navigation | frontend |

### Attention and navigation

| Event | When it fires | Surface |
|---|---|---|
| `link.open` | An external or workspace-document link is opened or the attempt fails | frontend |
| `sidebar.update_open` | The update dot's thread-open request succeeds, is declined, or fails | frontend |
| `update_card.plan_open` | A plan item title is opened from a transcript update card | frontend |
| `update_card.earlier_updates_expanded` | The `N earlier updates` transcript disclosure expands | frontend |
| `thread.seen_mark` | A mark-read or mark-unread request succeeds, is refused, or fails | backend |

### Navigation — open timing

Client-observed click-to-usable time. The server half of a slow open is already
readable from request timing (see `request_timing_daily`); these two events carry
the half that is not: how long from the user's action until the surface is
usable, and how much of that is render rather than fetch. One event per open.

| Event | When it fires | Surface |
|---|---|---|
| `navigation.thread_open` | The selected thread reached a usable first page plus its artifact list, was abandoned, or its load failed | frontend |
| `navigation.artifact_open` | An artifact leaf finished showing its document, was abandoned, or its load failed | frontend |

Measurement points:

- **thread_open** starts at the entry of the app's thread selection, before the scope
  lookups and the first message fetch it performs, so the network prefix sits inside the
  measurement. `click_to_first_page_ms` is measured after the Svelte `tick()` that follows
  the first message page arriving; `click_to_settled_ms` is the later of that and the
  thread's artifact list resolving. A list is awaited only when an artifact pane is on
  screen to report one: a collapsed section, a scope whose pane config excludes it, and
  the document-detail view all mount no pane, and those opens settle on the first page
  rather than being measured as abandoned. `artifacts_awaited` distinguishes the two
  populations — an open that waited on a pane's list, and one that never could have —
  because their settled times are not comparable. Both times are null when the open never
  got that far. A click on the thread already on screen is not an open and emits nothing —
  a ~0 ms sample would bias the percentiles this exists to read.
- **artifact_open** starts when the artifact leaf activates a document tab and stops
  when the viewer reports its content ready, after the tick that renders it. A tab
  whose content is already loaded settles immediately.

```sql
-- p50/p95 of thread open, by outcome and whether the open waited on an artifact
-- list, over one date range. The two waited/not-waited rows are not comparable:
-- an open with no artifact pane on screen settles on its first page by design.
WITH opens AS (
  SELECT json_extract_string(properties, 'outcome')                                AS outcome,
         json_extract_string(properties, 'artifacts_awaited')                      AS artifacts_awaited,
         TRY_CAST(json_extract_string(properties, 'click_to_first_page_ms') AS DOUBLE) AS first_page_ms,
         TRY_CAST(json_extract_string(properties, 'click_to_settled_ms') AS DOUBLE)    AS settled_ms
    FROM product_events
   WHERE category = 'navigation' AND action = 'thread_open'
     AND ts >= TIMESTAMP '2026-09-01' AND ts < TIMESTAMP '2026-10-01'
)
SELECT outcome,
       artifacts_awaited,
       COUNT(*)                           AS opens,
       QUANTILE_CONT(first_page_ms, 0.5)  AS first_page_p50_ms,
       QUANTILE_CONT(first_page_ms, 0.95) AS first_page_p95_ms,
       QUANTILE_CONT(settled_ms, 0.5)     AS settled_p50_ms,
       QUANTILE_CONT(settled_ms, 0.95)    AS settled_p95_ms
  FROM opens
 GROUP BY outcome, artifacts_awaited
 ORDER BY outcome, artifacts_awaited;
```

```sql
-- p50/p95 of artifact open, by kind and outcome, over one date range.
WITH opens AS (
  SELECT json_extract_string(properties, 'kind')                                  AS kind,
         json_extract_string(properties, 'outcome')                               AS outcome,
         TRY_CAST(json_extract_string(properties, 'click_to_content_ms') AS DOUBLE) AS content_ms
    FROM product_events
   WHERE category = 'navigation' AND action = 'artifact_open'
     AND ts >= TIMESTAMP '2026-09-01' AND ts < TIMESTAMP '2026-10-01'
)
SELECT kind,
       outcome,
       COUNT(*)                        AS opens,
       QUANTILE_CONT(content_ms, 0.5)  AS content_p50_ms,
       QUANTILE_CONT(content_ms, 0.95) AS content_p95_ms
  FROM opens
 GROUP BY kind, outcome
 ORDER BY kind, outcome;
```

These read the DuckDB `product_events` table, the documented taxonomy's home
store. The frontend wrapper still posts to PostHog (see *Configuration*), so
today the query answers from a mirrored or relayed copy; it needs no change
when the frontend relay lands.

### Composer

| Event | When it fires | Surface |
|---|---|---|
| `composer.focus` | Composer input focused | frontend |
| `composer.blur` | Composer input blurred (only if non-empty draft) | frontend |
| `composer.attached` | Attach button used | frontend |
| `composer.sent` | Send button or Cmd+Enter fired | frontend |

## Property conventions

All events carry these defaults (attached by the wrappers, never by callers):

- `project` — logical project name (`corsia`); lets multi-project PostHog instances filter
- `session_id` — CORSIA session id when known
- (frontend only) distinct_id is the PostHog user id, persisted in localStorage

Caller-supplied properties merge with defaults. Caller properties win on key collision.

### Per-category property additions

| Category | Additional properties |
|---|---|
| `turn.*` | `turn_id`, `model`, `provider`, `token_count` (when known) |
| `model.*` | `provider`, `model_id`, `reasoning_shape`, `previous_model_id` (on switch), `error` (on failure), `automatic` (true when CORSIA pinned or loaded a finished download itself) |
| `onboarding.account_created`, `onboarding.first_run_wake` | No caller properties. |
| `onboarding.first_model_selected` | `source_kind` (the model dispatch prefix only), `seconds_since_account` (integer or null). |
| `onboarding.first_turn_completed` | `seconds_since_account` (integer or null). |
| `onboarding.first_decision_answered` | `option` (the selected option identifier, truncated to 8 characters; for a pick-any decision, the chosen ids joined by commas, so up to four fit). |
| `onboarding.first_outcome` | `operation` ∈ `mark_done \| accept`; `seconds_since_account` (integer or null). |
| `cost.*` | `input_tokens`, `output_tokens`, `cost_usd`, `model` |
| `error.*` | `error_type`, `error_message` (truncated to 200 chars), `path` |
| `ui.modal_opened` | `modal_name` |
| `ui.tab_switched` | `from`, `to` |
| `ui.thinking_toggled` | `effort`, `reasoning_shape` |
| `composer.sent` | `char_count`, `attachment_count`, `edited` |
| `composer.attached` | `file_count` |
| `attachment.added` | `mime_type`, `is_image` |
| `link.open` | `target` ∈ `external \| workspace_document \| settings \| thread \| unsupported`; `outcome` ∈ `succeeded \| failed`; bounded `reason` ∈ `open_failed \| unsupported_scheme \| no_active_thread \| not_found` on failure |
| `sidebar.update_open` | `outcome` ∈ `succeeded \| refused \| failed`; bounded `reason` ∈ `not_opened \| open_failed` on refusal or failure |
| `update_card.plan_open` | No properties: card, thread and node identifiers and title remain out of analytics. |
| `update_card.earlier_updates_expanded` | No properties: count, card, thread and titles remain out of analytics. |
| `thread.seen_mark` | `mark` ∈ `read \| unread`; `outcome` ∈ `succeeded \| refused \| failed`; bounded `reason` ∈ `missing \| never_opened \| internal_error` on refusal or failure |
| `navigation.thread_open` | `click_to_first_page_ms`, `click_to_settled_ms` (null when the open never reached it); `outcome` ∈ `settled \| abandoned \| error`; `source` ∈ `sidebar \| link \| history \| other`; `artifacts_awaited` (whether the open waited on an artifact pane's list); `message_count_bucket` ∈ `0 \| 1-50 \| 51-500 \| 500+` (what the open actually loaded). No thread id, title, or content. |
| `navigation.artifact_open` | `click_to_content_ms` (null on error or abandonment); `outcome` ∈ `settled \| abandoned \| error`; `kind` (artifact type only, e.g. `document`) |
| `plan_dispatch.*` | `tool` (except `terminal_unknown`); `reason` ∈ `no_turn \| no_selection` (`unrecorded`); `outcome` ∈ `succeeded \| refused \| failed \| unknown` (`settled`, `settle_failed`); `count` (`terminal_unknown`) |
| `dev_tool.antipatterns_invoked` | `tool` (always `"antipatterns"`), `mode` ∈ `pre-commit \| ci \| manual`, `files_scanned`, `findings`, `severity_counts` ({`error`, `warn`}) |

Property names: lower_snake_case, ASCII. Avoid nested objects — PostHog
filters work best on flat properties.

## Configuration

Both wrappers read environment variables; nothing is hard-coded.

### Backend (`backend/infra/product_events.py`) — DuckDB-backed (Slice 0)

| Var | Default | Effect |
|---|---|---|
| `CORSIA_ANALYTICS_DISABLE` | `0` | Set to `1` to force-disable (tests, opt-out, CI smoke runs). Read per-call so `monkeypatch.setenv` takes effect without a singleton reset. |
| `CORSIA_ANALYTICS_PROJECT` | `corsia` | Logical project tag (also stored in the dedicated `project_id` column for index lookup). |

The DuckDB file lives at `~/.corsia/analytics.duckdb`. Schema +
indexes are bootstrapped lazily by `analytics_db.get_db()` on first
emit. No daemon, no Colima, no API key — the wrapper writes to a local
file synchronously; the `qh-query` CLI reads the same file.

The Slice-0 swap retired the `POSTHOG_*` backend env vars.

### Dev tooling (`tools/antipatterns/`)

Each tool calls into the backend wrapper, so it reads the same
`CORSIA_ANALYTICS_*` env vars. Heuristic env detection sets the
`mode` field on the emit:

| Var | Effect |
|---|---|
| `PRE_COMMIT=1` | Set by the `pre-commit` framework; tool reports `mode="pre-commit"` |
| `GITHUB_ACTIONS=true` or `CI=true` | Tool reports `mode="ci"` |
| (neither) | Tool reports `mode="manual"` (or `"pre-commit"` if `--staged` was passed to a tool that supports it) |

### Frontend (`frontend/src/lib/product_events.ts`) — current PostHog path

Frontend uses posthog-js. Vite-injected
env; **must** be prefixed `PUBLIC_` to be exposed to the client bundle.

| Var | Default | Effect |
|---|---|---|
| `PUBLIC_POSTHOG_API_KEY` | (unset) | Required; emits no-op without it |
| `PUBLIC_POSTHOG_HOST` | (unset) | Required with `PUBLIC_POSTHOG_API_KEY`; PostHog ingest URL |
| `PUBLIC_POSTHOG_PROJECT` | `corsia` | Logical project tag |

Provided via shell env at `vite build` time, or via a `frontend/.env`
file (also gitignored). When the relay-swap lands, these go away and
the frontend posts to `/api/product-events` (or equivalent) into the
same DuckDB store.

## Why fail-silent?

Emit calls sit on user-action seams: send a message, switch a model,
open a modal. If the analytics writer were down and the wrapper
propagated the error, a user clicking "send" would see a stack trace
for what is purely observability work. That's a user-visible bug for a
non-load-bearing side-effect — strictly worse than dropping the event.

Same logic applies to dev tools: if the anti-pattern scan's analytics
emit failed, the scan's pass/fail signal must still be authoritative —
a broken analytics writer is not allowed to brick a PR.

So both wrappers + the dev-tool emit shims:

1. Catch every exception at init AND at capture time
2. Use warn-once-then-debug: first failure of each kind logs at
   WARNING (visible in prod, audible to anyone tailing logs);
   subsequent failures of the same kind drop to DEBUG to keep logs
   readable when the writer is persistently broken
3. Stop retrying init once it fails (avoids hot-path latency when
   DuckDB is permanently misconfigured)

The retired subsystem-spec KPI monitor no longer turns these events into
threshold claims or GitHub issues. Use direct analytics queries for diagnosis;
a future automated monitor requires a current KPI contract.

## Adding a new emit point

1. Pick the category from the taxonomy above (or extend it here first
   if you genuinely need a new bucket — keep the table sorted).
2. Identify the seam — usually the same line where an existing
   `loguru.info` or store update lives.
3. Add the one-liner:

   Backend:
   ```python
   from infra.product_events import emit_product_event
   emit_product_event("turn", "completed", session_id=sid,
                      properties={"model": model_id, "token_count": n})
   ```

   Frontend:
   ```ts
   import { emitProductEvent } from '$lib/product_events';
   emitProductEvent('composer', 'sent', { char_count: text.length });
   ```

4. Document the new event in the property-conventions table if it
   needs category-specific properties.
5. If it is a KPI source event, update the current approved product contract.

## Local debugging

Backend (DuckDB-backed):

```bash
~/.corsia/.venv/bin/python -c "
from infra.product_events import emit_product_event, flush
emit_product_event('smoke', 'test', properties={'src': 'manual'})
flush()  # no-op under DuckDB but safe to call
"
```

Then read with `qh-query` or DuckDB SQL:

```bash
~/.corsia/.venv/bin/python -m qh_query 'SELECT * FROM product_events WHERE category=$1 ORDER BY ts DESC LIMIT 5' --param smoke
# or against the file directly:
duckdb ~/.corsia/analytics.duckdb "SELECT * FROM product_events WHERE category='smoke' ORDER BY ts DESC LIMIT 5;"
```

For the frontend, set both `PUBLIC_POSTHOG_API_KEY` and
`PUBLIC_POSTHOG_HOST` in a `frontend/.env` file, export
`CORSIA_BACKEND_URL` (the dev or test backend you started, for example
`export CORSIA_BACKEND_URL=http://127.0.0.1:<backend-port>`; `npm run dev`
refuses to start without it and never falls back to the production port), and
run `npm run dev`.
Frontend still uses PostHog pending the relay-swap follow-up. Browser console
`posthog.capture('smoke.test')` works only after some event has triggered lazy
init.

For the anti-pattern tool, every CLI run emits a `dev_tool.*` event with mode /
count / runtime attrs — useful for verifying that a
new emit point is wired correctly before relying on it for KPI
queries:

```bash
CORSIA_ANALYTICS_PROJECT=corsia PYTHONPATH=tools \
  ~/.corsia/.venv/bin/python -m antipatterns --paths backend/main.py
duckdb ~/.corsia/analytics.duckdb \
  "SELECT category, action, properties FROM product_events WHERE category='dev_tool' ORDER BY ts DESC LIMIT 1;"
```

## Structured plans (PLANS-AC41)

### What is state and what is evidence

Plan **state** lives in `plans`, `plan_nodes`, `plan_node_dependencies`,
`plan_node_events`, `plan_node_artifact_links` and `plan_mutation_receipts`.
It is transactional and audited, and it is the only
thing any decision or authorization reads.

Product events are **operational evidence**: fail-silent, post-state, and
**never state, never authorization**. No transactional behaviour depends on an
emit. `plan_node_events` is the opposite — append-only provenance that *is*
state — so a question answerable from it is answered there, not from telemetry.

### Where each AC41 metric is read from

AC41 is closed by naming a real source for each fact, not by adding a metrics
service. Most are already durable, and a durable row beats an emit: it is
transactional, exact, and survives an analytics store that was never enabled.

| Metric | Read from |
|---|---|
| node transition latency | `plan_node_events` |
| verification result | `plan_node_events.verification_result` |
| user corrections | `plan_node_events` |
| decision age | `plan_node_events` |
| stalled duration | `plan_liveness_wakes.fired_at` |
| wake admission (attempts) | `continuation_triggers.attempt` |
| delivery outcome | `continuation_triggers.state` / `.reason` |
| wake deferral and stall | `continuation_triggers.state` / `.deferrals` / `.reason` |
| wake cost | `llm_usage.cost_usd`, joined through `sessions` |
| dedupe | **not observable** — see below |
| rollup freshness | `plans.revision`, `plan_nodes.updated_at` |

Every query below runs against the real schema and is executed by
`test_plan_observability_contract.py`, so a fabricated table or column fails
rather than reading plausibly.

#### node transition latency, verification result, user corrections

One series per node. It is anchored at `node_created`, not at the first
`state_transition`, because the pending dwell — how long a node sat before
anyone picked it up — is usually the interesting one, and `LAG` over
transitions alone always leaves it NULL.

```sql
SELECT node_id,
       event,
       new_state,
       verification_result,
       (julianday(created_at) - julianday(LAG(created_at) OVER (
            PARTITION BY node_id ORDER BY created_at))) * 86400
         AS prior_state_seconds
  FROM plan_node_events
 WHERE event IN ('node_created', 'state_transition',
                 'review_rejected', 'node_restated')
 ORDER BY node_id, created_at;
```

`review_rejected` and `node_restated` rows in that series are the user
corrections; `verification_result` is non-null on the accepting transition.

#### stalled duration

From `fired_at`, which `plan_liveness._claim_fire` writes at the moment of the
stall. Not `window_started_at`: that is the rate-limit window start, is reset
when the window rolls over, and is NULL for a wake that stalled through
`deferral_exhausted` without ever delivering.

```sql
SELECT thread_id,
       (julianday('now') - julianday(fired_at)) * 86400 AS stalled_seconds
  FROM plan_liveness_wakes
 WHERE status = 'stalled' AND fired_at IS NOT NULL
 ORDER BY stalled_seconds DESC;
```

#### wake admission, delivery, deferral and stall

From `continuation_triggers`, the durable record of every trigger. The
`continuation` product event fires only inside `deliver_one`, after a delivery
attempt, so it carries `delivered` and `failed` and nothing else — deferrals,
suppressions and terminal cancels never reach it.

```sql
SELECT kind,
       state,
       reason,
       COUNT(*)        AS triggers,
       SUM(attempt)    AS admissions,
       SUM(deferrals)  AS total_deferrals,
       MAX(settled_at) AS last_settled_at
  FROM continuation_triggers
 GROUP BY kind, state, reason
 ORDER BY kind, state;
```

**Admission is `SUM(attempt)`**, not `state='delivered'`: a claim that succeeded
incremented `attempt`, and a wake that was admitted and then failed to land was
still admitted. Read delivery success as
`delivered / (delivered + failed WHERE reason='delivery_failed')`.
`deferred` with `deferrals` is deferral; `stalled` with `cycle_cap_reached`,
`wake_budget_exhausted` or `deferral_exhausted` is a stall.

#### wake cost

Not from `continuation_triggers`. `cost_estimate_usd` is a **structural zero**,
not an estimate taken at the wrong moment: no production writer ever sets it.
Every `TriggerRequest(...)` in `automation/continuation/sources/` omits the
field, the dataclass default is `0.0`, `triggers.enqueue` holds the only
statement that writes the column, and nothing updates it afterwards — so
`SUM(cost_estimate_usd)` is `0` over every trigger the system has written.

Cost is only knowable after a wake runs, and the one place it is recorded is
`llm_usage`:

```sql
SELECT s.id           AS thread_id,
       SUM(u.cost_usd) AS cost_usd,
       COUNT(*)        AS usage_rows
  FROM llm_usage u
  JOIN threads s ON s.id = u.thread_id
 GROUP BY s.id
 ORDER BY cost_usd DESC;
```

#### dedupe — honestly unobservable

**Dedupe is an absence and cannot be counted from `continuation_triggers`.**
When `enqueue` finds a live row for the same `dedupe_key` it returns that row
and inserts nothing, so a suppressed wake leaves one row and zero `suppressed`
rows — indistinguishable from a wake that was never duplicated.

Do not read `state='suppressed'` as dedupe, and do not add a `suppressed` write
to make this sentence true: that would fabricate a lifecycle state to satisfy a
document. Making it observable is a separate change — one
`emit_product_event("continuation", "deduped", ...)` at the existing choke point
where `enqueue` returns `created=False`.

#### rollup freshness

```sql
SELECT p.id,
       p.revision,
       MAX(n.updated_at)  AS newest_node_update
  FROM plans p
  LEFT JOIN plan_nodes n ON n.plan_id = p.id AND n.archived_at IS NULL
 GROUP BY p.id, p.revision;
```

Decision age uses the same `plan_node_events` series, from `decision_raised` to
`decision_answered` or `decision_invalidated`.

### `plans.close_next`

A closing mutation (request_review, fail, accept, accept_with_follow_up,
archive_node) returns a bounded `next` block: the task now holding its
executor's slot, a few of that executor's other open tasks, and the outcome of
an optional named next task. The plans domain's one emit point, in
`backend/plans/boundary.py`, measures whether that block replaces a whole-plan
reread: follow-through, the refusal mix, and how often an already-active task
fills the slot. It fires once per fresh closing mutation, after its write has
committed; a replayed mutation emits nothing.

| Property | Meaning |
|---|---|
| `operation` | the closing or parking operation |
| `named` | `absent` (no task named), `started`, or the refusal code: `unknown`, `not_open`, `lane_owned`, `user_assigned`, `other_executor`, `not_ready`, `slot_held` |
| `selected_how` | `started`, `auto_selected`, `running`, or `none` (slot empty) |
| `open_total` | the executor's other open tasks in the plan |
| `open_listed` | how many of those the block lists (at most 3) |
| `executor_is_actor` | whether the closing actor is the closed task's executor |
| `description_carried` | whether the result carried `selected_description`, the handed-over task's description, beside the block |
| `description_bytes` | the carried description's serialized bytes, `0` when none was carried |
| `description_cut` | whether the carried description was cut at its ceiling |

Codes and counts only. No node id, title, criteria, reason or description — the
block is the agent's answer, never a copy of it in the analytics store. Adding
an emitter anywhere else in `backend/plans/` fails
`test_plan_observability_contract.py`.

The three description properties are read from the fact the close carried, as
its writer built it, never from storage. A fact that cannot be measured leaves
all three `null` (unknown) and the rest of the event intact, and logs a
warning naming the operation. A task that
reaches its executor through somebody else's write arrives as a work-selected
wake instead, which carries the same `selected_description` and emits no
product event, so what the wake route carries is not measured.

### `plan_dispatch.*` — tool-call ownership

`plan_tool_dispatches` is the state: which started call is recorded under
which attempt, and how it settled. The events are evidence the rows cannot
give: how many calls ran without a row and why, when recording or settlement
failed, and how many calls a Turn terminal had to close as unknown (the
`plan_dispatch.terminal_unknown` product event). One module outside the plans
domain owns them, `backend/infra/plan_dispatch_events.py`, and writes every one
off the event loop, because a product event is a synchronous analytics write:
the tool boundary (`backend/tools/dispatch_ownership.py`, reached from
`backend/tools/recorded_call.py`) emits in the database
worker right after the write it reports commits or fails, and the terminal
settlement driver hands the count to that worker after the terminal write
commits. Plans' own tools record no row and emit nothing. Tool names, reasons,
outcomes and counts only; never tool arguments or results.

## Cross-references

- `sprints/2026-05-13-quality-foundation-sprint-04/PLAN.md` — Sprint 4 deliverables (this slice is #3)
- `sprints/2026-05-13-quality-foundation-sprint-05/PLAN.md` — Slice 0 PostHog → DuckDB reversal (DECISIONS-LOG #26)
- `backend/eval/otel.py` — OTel emit surface (distinct from product events)
- `backend/infra/analytics_db.py` — DuckDB substrate (schema + connection singleton)
- `.env.example` — required env vars template
