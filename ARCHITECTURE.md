# CORSIA architecture map

This file describes the implemented code shape. It is not work authorization,
a roadmap, or a product contract. Current scope comes from the latest direct
user instruction, the named current plan, and its approved PRD + acceptance
criteria.

## System shape

CORSIA is a self-hosted, model-agnostic agent platform. One backend owns
conversation history, context composition, scoped skills, artifacts, events,
delegation, and provider dispatch. A SvelteKit client communicates with the
FastAPI backend over REST and WebSocket. SQLite is the operational source of
truth for conversations, messages, settings, artifacts, routing state, and
other runtime records.

Two axes stay separate:

1. **Execution provider:** `native`, `cc`, or `codex` owns the agent loop.
2. **Inference provider:** a native turn resolves a local or cloud model under
   `backend/llm/`; it does not create a second execution lifecycle.

Inbound user turns, delegated messages, and routed events converge on shared
send and dispatch services before entering the selected execution provider.

## Code map

| Area | Path | Responsibility |
| --- | --- | --- |
| API | `backend/api/` | REST endpoints, auth boundaries, project/files/settings surfaces |
| Turn and context core | `backend/core/` | turn lifecycle, context collection/composition, message handling |
| Inference | `backend/llm/` | model catalog, routing, provider clients, streaming, usage/cost |
| External execution | `backend/subagents/` | Claude Code and Codex runtime adapters and continuity |
| Review lifecycle | `backend/reviewer/` | GitHub reviewer inputs, PRD mapping, verdict/projection handling |
| Coordination | `backend/events/`, `backend/registry/` | normalized events, artifact identity, owner routing |
| Agent tools | `backend/tools/` | filesystem, shell, browser, document, and CORSIA-native tools |
| Runtime infrastructure | `backend/infra/` | configuration, database, state, persistence helpers, recovery |
| Memory and jobs | `backend/memory/`, `backend/jobs/` | summaries, knowledge, scheduled maintenance |
| Transport | `backend/ws/` | WebSocket connection and message transport |
| Evaluation | `backend/eval/`, `evals/` | scenario execution, scorers, and evaluation fixtures |
| Frontend | `frontend/src/` | SvelteKit application, stores, API client, and UI primitives |
| Operations | `scripts/` | supported start, dev, verification, and maintenance commands |

Deeper callable maps live in `BACKEND.md` and `FRONTEND.md`. Visual work also
follows `frontend/DESIGN-SYSTEM.md`.

### Verified ONNX assets

`backend/core/onnx_asset.py` is the single owner of verified ONNX asset
manifests, source pins, file hashing and stat caching, session construction,
and self-tests. `backend/core/onnx_child.py` owns the isolated child lifecycle:
network blocking, child-side and parent-polled RSS ceilings, request timeouts,
and shutdown escalation. Each feature defines only an `OnnxAssetSpec` and a
module-level scorer. `corrections_classifier.py` provides the corrections
specification and converts generic score replies into its recorded value.

### oMLX public authentication

`backend/llm/providers/omlx_public_auth.py` derives a public Bearer header from
one registered server identity, its current metadata key, and its registered
endpoint set. Public consumers carry that identity to request time; managed
sweep and freshness recovery reuse `omlx_public_probe.py`. This path never uses
a cfg host to authorize a stored key. Admin routes remain separately owned by
`omlx_endpoints.admin_request` and its cookie session.

### System tier projection

`backend/infra/system_package.py` locates immutable System source content. At
startup and before System skill resolution, `backend/core/system_projection.py`
materializes its manifest-verified files into `<workspace>/.corsia/`; registry
and context rows advertise those workspace-readable paths while delivery still
reads package bytes. The projection is generated, read-only in effect, and
restores drift rather than accepting edits as System content.

### Foreground shell invocation identity

`infra/state/context.py` owns a task-local invocation scope. Bridge, native
and checkpoint callers set it around registry execution and reset it in
`finally`; checkpoint execution clears it. The registry privately forwards it
to bash before the executor hop. `tools/shell_environment.py` assembles the
backend-owned thread/Turn identity and sanitized environment; shell guards
re-export its existing environment function. Inherited invocation values are
removed, and background/long-running calls export none.

Claude Code supplies an MCP-minted invocation UUID; Codex uses its provider
call id and native uses its tool-call id. These values alone are not globally
unique or proof of tool success. This seam carries identity only: it adds no
outcome consumer, transport, credential, or corroboration state.

### State versus operational evidence

Two surfaces record what happened, and confusing them is how a system starts
authorizing itself from telemetry.

**State** is the database. For plans that is `plans`, `plan_nodes`,
`plan_node_dependencies`, `plan_node_events`, `plan_node_artifact_links`,
`plan_mutation_receipts`, `plan_markdown_imports`, `plan_node_attempts`,
`plan_tool_dispatches` and `plan_node_holds`. It is transactional, audited, and
the only thing any decision, authorization or projection may read.
`plan_node_events` in particular is append-only provenance: it is state, not
telemetry, and it is where the history of a plan actually lives.

Execution state lives in the last three. A task is *selected* exactly when it
has an open `plan_node_attempts` row, enforced as one open attempt per plan and
executor by a partial unique index; attempts alone select work, and every
transition that changes eligibility reconciles the plan in the same transaction
(`plans.admission.reconcile_plan`), so the next eligible task is selected
without a manual start or another Turn. A delegated step's wait is read from
its worker, so the worker's side reconciles the delegator: every worker-plan
reconciliation and the worker's Turn end (`plans/delegated_selection.py`).
The step taking its slot back is state; the parent hears about its
worker only through the notification matrix. A closing mutation (request_review,
fail, accept, accept_with_follow_up, archive_node) may start one task the caller
names through the same activate/start writers, after its own release and before
reconciliation, and returns a bounded `next` block composed by
`plans/close_next.py`, which reads the selection owners and is not one; nothing
pending is started unasked. `plan_tool_dispatches` records a tool
call that actually started under the executor's valid selection, so an
in-flight effect keeps an owner: its open row keeps that attempt's id for
attribution when the call settles. It never holds the slot: parking a task
releases its attempt at once, even mid-call, so a lost settlement cannot
strand the executor. Plans' own tools (tool
catalog category `plans`) record no dispatch, because their plan effect commits
in the same write. A dispatch never selects, binds or refuses a call — Plans is
not a tool-boundary authorization, and each tool still passes its own
validation, identity, restart, host-access and repository-policy boundaries.
The tool boundary and the Turn-terminal settlement driver report dispatch
outcomes as `plan_dispatch.*` product events (evidence, never state; see
`docs/observability.md`). `plan_node_holds` carries every pause and blocker with its accountable owner.
Plans is the only selection authority — the tool boundary and the UI read these
facts and never compute their own. A designated project conductor may append a
top-level task to another agent's existing plan in its project, and the workspace
conductor may do so across projects; no conductor may otherwise drive that plan.
The append stays pending and never selects or wakes the receiver. Its
`plan.conductor_add` creation event names the conductor to wake once when the
task settles, while foreign-authored send-back steps remain ordinary handoffs.
A Turn's plan columns are write-once
attribution of the first attempt its cost accrues to, never execution
authority; they are written only at Turn start (`infra/thread_inputs.py`) and by
`infra/turn_plan_binding.py`, the enrolled owners for those `active_turns`
transitions.

**Operational evidence** is `emit_product_event` (`backend/infra/product_events.py`),
written to DuckDB. It is deliberately **fail-silent**: every failure is logged
and swallowed, because committed state remains authoritative. Therefore no
transactional behaviour, authorization decision or user-visible outcome may
depend on an emit, and every emit happens after the state it describes has
settled. Evidence is for humans asking how the system behaved; it is never a
second copy of state and never an input to the system's own decisions.

**Durable measurement** is the `outcome_events` table in the main SQLite
database, the one store measures may rest on. Today it holds *raw receipts*:
facts received from outside (a GitHub delivery, mapped by
`backend/events/connectors/github_outcomes.py`), keyed on a source-stable
`dedupe_key`, written first-write-wins by `record_outcome_event`
(`backend/infra/outcome_events.py`), and never rewritten; withdrawal leaves a
tombstone. A delivery's facts are written by that delivery's own admission
transaction (`record_delivery_for_admission` in
`backend/events/connectors/github_admission.py`): the delivery row and its
facts commit together or not at all, and a payload the mapper or the store
refuses is counted `mapping_failed` in the same transaction. Raw facts carry no
agent, project or attribution; a pull request's author login and declared owner
are recorded as observed, unverified facts. Derived rows (attribution, the
delivery-container relation) are not written yet and will have their own
writer and trigger.

One imported fact type rests on the same table: `measurement.size_rating`, an
offline size-class rating of one accepted outcome (`backend/core/size_rating.py`).
It is written only by the operator import in `backend/core/size_rating_import.py`
(CLI `backend/scripts/size_rating_import.py`, guide `docs/size-rating-import.md`)
under a `dedupe_key` derived from the payload and the newest observations it
supersedes, so a retry of the rating already served is a no-op and any other
valid rating, an earlier one re-imported included, adds a new fact. The newest observation governs;
different payloads tied at that time are a conflict. The Measurement series
`estimated_human_hours_per_active_hour` (`backend/api/measurement_size.py`)
reads these facts as size-class midpoints over active hours, serving coverage
and subtotals every week and naming why a rate is withheld.
Whether operational `product_events` should later converge into this shape is
deliberately deferred; until then they stay separate stores with separate
contracts, and nothing adds a second lifecycle to either.

### Plans public interface

Code outside `backend/plans/` reaches Plans only through `plans/public.py`;
every other `plans` module is internal and may change shape without notice.
The facade re-exports what outside callers need today, under public names where
the defining module's name is private or would clash (`record_tool_dispatch`,
`report_usage_defect`). A new outside caller adds its name there with its first
use. A test that replaces a function an outside caller uses patches it on
`plans.public`, because that is where the caller looks it up.

Activity scope lists use the versioned compact `activity_scope_index` projection
through `/api/plans/activity/scopes/...`. It carries canonical display,
liveness, remaining-work, attention, and ordering facts only. The version-2
operator thread projection remains the sole node-tree/detail contract; compact
and full representations have independent keys and ETags. Both frontend readers
share the authenticated conditional-read boundary in `api/planRead.ts`, with
each representation retaining its own decoder. The original full workspace and
project v2 routes remain supported for compatibility.

Activity refreshes are spaced on the client. It starts at most one request per
Activity key every two seconds; a refresh asked for inside the gap joins one
trailing read, so the latest state wins and the last change is kept
(`plans.svelte.ts`, `planRefreshSpacing.ts`).

`activity_facts` selects minimal node columns and orchestrates owner-local set
reads: `node_waits` owns holds, prerequisites and watches; `admission_projection`
owns intrinsic causes and attempt validity; `rollup` owns rejected-round
suppression; `artifact_links` owns linked-attention scalars; `decision_projection`
uses its existing persisted JSON parser and batched answerability for eligible
linked decisions, returning only lifecycle facts. Parsing is linear in those
metadata bodies, which never leave that owner. Shared execution, display,
deadline and header reducers preserve the distinction between display quietness
and own-plan liveness quietness. No full node presentation is built for Activity.
The frontend uses one query lifecycle with separate typed Activity/operator key
families and checked cache admission; child key sets accept operator detail only.

`plans.lifecycle` imports `automation.plan_liveness` while it loads, so any
outside module on that load path imports `plans.public` only under
`TYPE_CHECKING` or inside a function; otherwise it would read a half-built
facade. `backend/tests/test_plans_public_boundary.py` enforces both rules.

### Plan reads cost the open work

A plan read costs the plan's open work, a recent window and its depth, never its
settled history. `plans/open_tree.py` is the one loader: it reads open rows
through `idx_plan_nodes_open`, the settled leaves of the recent window, and the
ancestors those rows climb through, and nothing else. What the unread settled
rows would have said comes from stored aggregates carried on every row
(`child_in_work`, `folded`, `sub_done`, `sub_failed`, and per-level
`child_folded_*` counts by tone). An equivalence property test holds its
`Shape` equal to `plans.node_states` run over the full row list.

`plans/aggregates.py` is the only writer of those columns
(`infra/plan_aggregates_schema.py` defines them). `apply` runs inside the two
writers of an aggregate input, `service.touch_node` and node creation, and
updates one statement per level up to the root. The fold count always
propagates; done, failed, closed and in-work counts stop at a retired node. It
never writes `revision` or `updated_at`, and the plan revision trigger ignores
these columns. `plans/aggregates_rebuild.py` computes the same figures from
scratch (the schema 106 backfill and test fixtures), and
`plans/aggregates_recount.py` is the independent check both must equal. The
migration asserts it before committing, and the tests use it as their oracle.

The server alone decides what folds. A pane read reports, per listed level, how
many folded rows sit beneath it and how many it left out
(`plans/open_folds.py`); `plans/folded_reads.py` pages the rest one level at a
time, newest first, from `idx_plan_nodes_folded`, and `read_path` places one
node the client has not loaded. The client computes no fold or count of its
own. The post-write status drain finds touched plans with one read per table
(`status_context.touched_plans`). Its branches join with `UNION ALL`, because a
plain `UNION` lets SQLite 3.53 scan every `plan_nodes` row on every plan write.
`test_plan_write_cost` pins that query plan.

### Delegated workers join a plan task

`delegate_task` (`backend/tools/delegate_task.py`) hands its new worker a plan
task: the caller's current task by default, another one named by
`plan_node_id`, or none when that is `'none'`. `plans/delegate_attach.py`
chooses the task before the spawn, so a named task that cannot be handed over
is refused with nothing started. After the spawn, it attaches the worker
through the same `attach_spawned_thread` mutation a hand attach uses. The
result's `plan_task` field says what happened and how to change it next time.

A worker the caller already started is reused through `delegate_send`
(`backend/tools/delegate_send.py`) with the same `plan_node_id` and the same
attach path, but only for a task the caller names: a send is often just a
question, so it never takes the caller's current task by default. The named
task is checked before the message goes (the target must be the caller's own
worker holding no other open task). Without `plan_node_id`, a send to such an
idle worker carries a `plan_task` note saying its reply arrives as chat.

### Reading and depending on another plan

`plan_read(thread_id=...)` reads another same-user, same-project thread's plan
through the same scope check as the operator read, never creating one; a
denial says why. A dependency may name a prerequisite in that plan. The edge
lives on the dependent, so `plans/cross_plan.py` is the one place that tells
the dependent's plan when the prerequisite settles or leaves: called from
`service._transition` (accepted or failed for good) and
`dependencies.drop_edges_for_node` (closed or archived), inside the
prerequisite's write. It reconciles the dependent's plan and sends one
`plan.prerequisite_settled` wake per dependent, prerequisite and outcome
through `plan_wake.append`. A dependent in the prerequisite's own plan hears
only a failure or a close, and only when someone other than that plan's owner
caused it; a close still drops the edge, so the dependent is ready.

### Setting a task aside

Pause is the user's (`plans/pause_authority.py`): an agent's `pause` or
`pause_node` is refused with one line naming what to do instead. An agent
schedules a task instead (`plans/scheduled_wait.py`): `arm_watch` with a
`reason` and a deadline, any distance out, arms an ordinary watch whose
`watch_armed` event carries `scheduled.reason`. The row reads the date, then
the reason, and when the deadline passes the watch-expiry wake names the task,
how long it was set aside and why. Schema 59 moved every open pause an agent
had placed on its own plan to such a schedule, spread over the following week.

### Every wait ends

An event wait (`plans/event_wait.py`, called from `watch_operations.arm_write`)
is armed as a deadline watch that keeps its artifact and expected event, so
the event still ends it early (`plans/watch_resolution.py`) and the deadline
ends it otherwise: 24 hours when none is named (or the deadline of the same
wait already armed on the task), and a deadline past 7 days is cut to 7 days
with the reason in the reply. An event wait with no artifact is still
refused. Its row reads "Waiting for <event> until <deadline>", and its expiry
wake says the event did not arrive and what to do. Schema 60 gave every armed
plan event wait a deadline in place (`automation/watch_deadline.py`), keeping
its id and writing `watch_deadline_given` under `migration.59_to_60`, which
tells no one (`plans.plan_events.from_schema_step`).

A delegated worker that stops while its only open work is held by the user
sends its parent one `plan.child_stopped_on_user` wake
(`plans/delegation_turn_end.py`); a plan paused by the user does not, since
the parent already gets its digest. A worker's blocker is keyed on the step
and its reason (`plans/delegation_decisions.py`): arming it again while its
last wait is armed or expired tells the parent "renewed until <date>"
without a wake; a blocker raised again after it cleared wakes the parent anew.

### Per-node cost roll-up: working and waiting time

`plans/node_cost.py` is the one place per-node facts become a tree total. Each
node's `own` figure holds tokens, model cost, usage-recording failures,
`execution` (Turn seconds from `active_turns`) and `waiting` (seconds). The
`rolled` figure adds every descendant and the rolled total of each attached
child plan, folding each plan exactly once. Waiting is wall clock, never spend:
it rolls up beside execution and is never added to tokens or cost.
`plans/node_waiting.py` derives it on read with no stored column. A node waits
while it is open and has an armed watch (other than a task its agent set
aside, whose `watch_armed` event carries `scheduled.reason`), an unmet
dependency (edge events intersected with the prerequisite's not-accepted
state stretches), or is in `review_requested` with a linked GitHub
pull-request artifact. Overlaps count once. The user's time is cut out,
whoever armed the wait: user-owned holds, whole-plan pauses (from
`plan_events`), a decision on the node while its review step is in
`review_requested` (the condition `decision_projection.answerable` settles on;
ended earlier where its attention was cleared out of band; found by source
node through the partial index `idx_artifact_identities_decision_source`), and
a dependency on a step assigned to the user while that step is open (closed or
failed, it is an unmet dependency again). So is time after settling. Every read is a
chunked `IN (...)` batch, a fixed count per call. The status read shows a
node's own `waited_seconds` in the ages of running, review and waiting rows.
`plans.public.open_wait_intervals` returns the same intervals per thread,
unioned over that thread's own plan nodes and clipped to a window, for a run's
open time (the caller passes descendant threads; there is no delegation walk).

### User presence

Whether the user is actually looking at a thread is state with one owner:
`backend/infra/presence.py`. It alone holds the in-memory per-socket claims,
document-open capabilities, and live-read verdict. Its durable entry, exit and
finalization writes (`thread_presence` for the current visit, `presence_visits`
for settled history) live in `backend/infra/presence_records.py`, called only by
the owner (keep-alive, mark-seen and the restart exit too); `presence.py` itself
keeps claim orchestration, the sweep and the thread-delete write. `backend/infra/presence_view.py` derives the `User
presence` line, thread read and `user_presence` tool payload from that owner.
The browser client
(`frontend/src/lib/ws/presence.ts`) only reports `presence` frames over the
existing socket, and `backend/ws/presence_control.py` hands them straight to the
owner: a presence frame never starts, steers or queues a Turn. Presence is
never emitted as a product event, and no other module may compute or write it.

Window focus is a second, separate fact the same owner records for Measurement
attention (`backend/infra/presence_focus.py` holds its in-memory state). Each visit carries the spans in which a CORSIA window had focus
(`focus_spans`, plus the open span's `focus_since`); every blur ends focus, and
a focus change never touches presence, ownership or the visit's exit. A visit
whose `focus_spans` is NULL predates focus tracking and counts whole. Measurement
expands the spans only in the attention allocator's `presence_focus_spans`; the
visit rows themselves stay one per visit, so `n` still counts visits.

Reading every span costs time quadratic in how often a user switches windows, so
Measurement reads the finished past from a cache of the same union: the focus
rollup (`backend/api/presence_rollup.py`, tables in
`backend/infra/presence_rollup_schema.py`) keeps, per 15-minute UTC bucket, the
seconds during which exactly one set of threads was focused. `api.usage_rollup`
runs its refresh beside the usage refresh (compute on the bulk lane, one short
`write_transaction` per step). It covers only what it can prove: `built_through`
never passes two hours ago or the start of a visit still open, and a trigger on
`presence_visits` moves it back when a row is written below it, so a read is
exact whatever the rollup's state and falls back to the spans when it is not
usable. `backend/api/measurement_presence_rollup.py` is the one reader.

### Agent endpoints

Whether a thread can be listed as a contact and reached by native send is one
rule: `native_endpoint_gap` in `backend/infra/delegation_scope.py`, which
returns a reason or `None`. A registered agent qualifies while active and
available. A designated conductor qualifies through its designation alone
(re-read on every send), still requiring an available, active thread of the
same user, so a conductor is never registered a second time as an agent to be
reachable. The designation is scoped: the CORSIA agent is the one the
workspace designates, and a project conductor is the one its own project
designates, so a thread designated for one project is not an endpoint in
another even though `PUT /api/conductor` and `PUT /api/corsia-agent` validate only account ownership.
Cross-scope send (`backend/tools/delegation_send_context.py`) checks each
endpoint against the live designation of the scope that owns that row, and
`validate_send_target_scope` still decides which pairs may cross scope at all:
only a pair that includes the designated CORSIA agent, so
project-to-project sends and ordinary chats stay refused. The roster's
CORSIA-agent contact (`backend/core/agent_roster_queries.py`) calls the
same rule with no project designation, so it lists the CORSIA agent
exactly when a send can reach it and never grows a project conductor; neither
caller keeps its own gate. When a project agent cannot get the contact, the
roster says why instead of omitting it.

### Repository policy fence

`backend/infra/repo_policy_transition.py` is the one process-local owner of the
mutual exclusion between repository *effects* (any tool call that may read or
touch a working repository) and repository *policy transitions* (attaching,
detaching, pinning or checkout-protecting a repository). The fence is global on
purpose; it is not scoped per repository, and effects, transitions and owned
background processes stay mutually exclusive in every direction.

`ToolRegistry._authorized_execute` wraps every tool call in `admit_repo_effect()`
except the tools whose own execution enters the stronger transition:
`_REPO_TRANSITION_OWNERS` in `backend/tools/registry.py`, matched on the
resolved post-dispatch name so a `tool_call` dispatch matches too. Those two are
`project_create` (through `api.project_scaffolding.initialize_project`) and
`project_repo_register` (through `api.project_repos.register_source_repo`);
admitting their own effect would only make each collide with its own
transition. The set is pinned in test to those two tool objects, so a rename or
a re-point cannot silently widen the exemption. Everything else is still
admitted as an effect and still refuses against an active transition. The
detach and policy-update routes take the same transition from the HTTP surface,
which is not a tool path and keeps its own admission, and
`project_repo_register` still refuses a non-Git folder before it ever reaches
the transition.


## Context and skills

CORSIA composes context every turn:

- system context from the workspace `context/` directory;
- project context from the project `context/` directory;
- Agent and Conductor context selected from `context/roles/<role>/` at those
  existing scopes, with Conductors composing Agent context before their additive layer;
- optional thread context from the thread's `context/` directory;
- scoped skills discovered from `skills/<name>/SKILL.md`, including selected role
  directories, and pulled on demand;
- current conversation, artifact, and runtime context.

The directories are the normal source of truth for boot files. Provider-native
boot files are thin connectors, not parallel context authorities.

### Trial lessons

A persistent agent's own unproven rule about how to work is kept as a trial
lesson: an ordinary skill file in that agent's own thread `skills/` directory
whose front matter carries a `corsia.lesson` block (owner, state, review date,
review reference). The file is the one record of the trial; there is no lesson
table and no second registry.

- **Writing.** `continuity_edit` `place` with verdict `trial` goes through the
  existing placement queue and worker. `backend/core/lesson_admission.py`
  checks the answer, and `backend/core/lesson_outcomes.py` writes the file
  through the guidance writer and registers it as an artifact. A hypothesis
  that rests on a person's words without a derivation and a record of those
  words is kept as a captured hypothesis under the thread's
  `context/captures/` and is never delivered. A temporary worker keeps no
  trial lessons.
- **Review.** `backend/plans/lesson_reviews.py` is the only writer of a
  trial's review: a pending task on the owner's plan, linked to the file and
  scheduled for the review date. It takes no executor slot before that date.
  The owner retires, promotes, extends, or revises the trial; nothing does so
  automatically.
- **Index membership.** `backend/core/lesson_index.py` decides, once per
  skills-index install, which trials are offered: the file is in its owner's
  own skills, says `trial`, its date is ahead, its owner holds an open review
  of that exact version, no ordinary skill has its name, and the lesson index
  budget has room. Anything else is listed as omitted with its reason. The
  installed index records the offered lessons; the per-turn manifest keeps only
  each lesson's id and behavioral hash.
- **Evidence report.** `backend/core/lesson_report.py` projects existing review
  history, validated current files, installed boundaries, per-call manifests,
  and canonical read results. The deferred `lesson_report` tool and Measurement
  HTTP endpoint share `lesson_report_access.py`; neither writes nor records a
  delivery. A private read-only snapshot collects database facts and closes
  before filesystem scans or immutable Git-object reads. Exact path/raw-hash
  identity may establish a body read; ambiguous or missing lineage stays unknown.
  Current files and packaged System availability do not establish delivery;
  unresolved project roots, System projection and role-skill coverage are named.
- **Context accounting.** `backend/infra/lesson_usage_attribution.py` annotates
  the selected frozen Skills inventory through a usage-level estimate sibling.
  It changes no installed bytes or hashes. Descriptor estimates remain part of
  Skills; body reads remain history. The Context sheet and Measurement Learning
  detail use existing document and review navigation.
- **Limits.** The index is frozen at install, so a new, changed, or expired
  trial takes effect at the next install and never in a running conversation.
  Offers and supported reads do not establish opportunity, application or benefit.
  Incomplete historical populations and per-version costs remain unavailable.

## Managed background processes

`backend/host_access/processes.py` is the sole live owner of agent-created OS
process groups. Every background child waits behind the one-byte pre-exec gate
in `host_access/process_launch.py`; `ProcessOwner` binds its identity and the
injected transition bridge durably inserts `managed_process_handles` before the
gate can release the requested command. Long-running `bash` is protected-scope
only, requires a description, is limited to two live handles per thread and six
per instance, and ends at 24 hours. Its bounded head/tail logs, scoped
status/log/list/stop tools, stall detection, and exactly four wake outcomes
(`stalled`, `completed`, `failed`, `interrupted_by_restart`) are owned by
`host_access/managed_lifecycle.py`, `automation/managed_process_events.py`,
`automation/managed_process_recovery.py`, and `infra/managed_process_store.py`.

Explicit thread deletion remains coordinated by `api/thread_deletion.py` and
uses `core/thread_family.py` as the canonical family writer. The process owner
first closes family admission, proves every captured group terminal, and joins
its spool, watcher, and ordered durable transition. The family-delete
transaction then replaces active handles with immutable
`managed_process_retirements` rows. Those tombstones suppress late callback
resurrection and own validated log leaf names until the shared immediate,
startup, or nightly drain removes both files. A running or death-unverified
durable row without a verified live owner refuses deletion; an aborted delete
reopens admission and retains its terminal row and queued event.

Schema 67 introduced both managed-process tables. They intentionally have no
thread foreign key: active rows must survive backend restarts, and retirement
rows must survive the thread they finish deleting. This lifecycle does not
relaunch work after restart or provide general live-thread retention; global
handle retention remains separate.

## Runtime and checkout isolation

An instance is a checkout. `scripts/corsia` operates exactly the checkout it
lives in: the script's own location resolves the backend, interpreter, env
file, port, state root, and launchd job, so a copy running from a worktree
cannot address another checkout's instance. There is no environment selector
and nothing to choose between; a `CORSIA_DIR` naming a different directory is
refused rather than honored.

The ordinary machine has one serving checkout, one launchd service, and one set
of operational databases under the platform state root (`corsia.db`,
`analytics.duckdb`, `uploads/`). The serving checkout tracks whichever branch
its owner has pointed it at. A checkout's launchd job is named by
`CORSIA_SERVICE_LABEL` in its own `.env`, defaulting to `com.corsia.prod`; a
plist under that name whose `WorkingDirectory` is a different checkout is not
this checkout's service, so lifecycle verbs take the direct-process route and
install or uninstall refuse to touch it. A second checkout that wants its own
durable service pins its own label and port.

Identity is proved before effects, never inferred. Direct-process verbs check
that the listener on the port runs from this checkout, and a listener that
cannot be attributed is refused rather than assumed. A backend verifies the
installed definition only when launchd started it, which
`CORSIA_SERVICE_GENERATION` in its environment is what says.

Agents work in linked feature worktrees created from the standalone worker
clone; they do not develop in, repoint, or switch the serving checkout. Tests
and evals run against disposable state seeded into an explicit
`CORSIA_STATE_DIR` (`scripts/seed-worktree-db`), never against the operational
database. The backend log lives in that instance's state root, so two checkouts
on one machine never write to, truncate, or tail one shared file.

The supported lifecycle is through `scripts/corsia`. Do not infer checkout
paths or switch branches from a running process.

## Scoped audit reads and host-access refusals

`backend/infra/audit_reads.py` owns the fixed state-database audit metrics and
one caller-user/project predicate. The authenticated HTTP route and agent tool
call that same service; the agent tool gets its current thread from the tool
registry, never from model arguments. The service opens a dedicated SQLite
`mode=ro`, `query_only` connection, binds actor and rows to one read snapshot,
and bounds the time, rows, and response bytes.
It returns aggregate checkpoint health, same-scope lease outcomes, or the top
ten stored-message pressure rows with an explicit `rank_limit` of 10. All metrics and the caller use the canonical
`thread_available_sql` predicate. Rows with an installed checkpoint take their
current-epoch boundary from `checkpoint_queries.current_installed_through_seq`,
which shares the exact selection rule with `current_installed`; rows without one
are labeled `no_checkpoint` and count full stored history. These numeric sizes
are proxies. An optional UTC window limits pressure ranking to threads whose
`updated_at` falls in the window; the response repeats the applied range, and
timeout guidance tells the caller to supply or narrow it. `history.projection`
remains the owner of exact provider replay. Lease outcome
categories use `failure_reasons.safe_rejection` for the same fallback as the
holder tool. The service exposes no SQL, database path, message content, raw
checkpoint error, log line, or DuckDB event property. A backend
outage still needs the separately reviewed lease path for authorized recovery.
`audit_read.succeeded` and `audit_read.denied` events contain only a fixed
metric name and, for denials, a fixed error category.

The host-access application reviewer stores its complete verdict in the
existing lease review envelope. Its strict `reason_category` is the only
reviewer-authored value the agent-facing `request_host_access` result projects:
the fixed map in `host_access/failure_reasons.py` renders it as guidance,
while private free-text reasoning stays in the same-user lease record.
`reason` remains a bounded terminal code; legacy missing categories become
`unknown`. `rejected` is a completed review verdict;
`blocked` remains a review or activation failure. No second decision state or
automatic exception is introduced.

## CORSIA's own logs for conductors

`backend/infra/otel_logs/` captures the backend's own log records so every
designated conductor (workspace or project) can read them through the native
`log_read` tool without a host lease. `capture` adds a loguru sink (INFO and
above) and a root stdlib handler (WARNING and above, never `uvicorn.access`)
that copy each record onto a queue bounded at 2,000 records and 8 MiB; a full
queue drops and counts, and the sinks never block, raise or log. Records from
`infra.otel_logs.*`, the writer thread or a re-entrant emit are refused, so the
pipeline cannot feed itself. `backend.log` stays byte-identical: no existing
sink is removed, and because a root handler silences `logging.lastResort`,
capture attaches `lastResort` itself when root had no handler and detaches
exactly what it attached.

`writer` is one daemon thread holding the only write connection to
`$STATE_ROOT/otel_logs.db` under an exclusive `flock`; that file is never
`corsia.db`. `store` checkpoints the WAL explicitly and keeps the footprint
under 512 MiB and 14 days: before each insert it applies a free-disk floor,
the WAL threshold, then a soft size limit (prune oldest first; drop the batch
if still over). A schema-version mismatch recreates the file, because logs
are evidence, not state. `lifecycle` starts the pipeline in
`app_startup_runtime` after database initialization and stops it after the
product-event flush. Capture is off whenever `CORSIA_TEST_MODE=1` (pytest and
worktree test instances), which the supported production and port-3001
launchers strip; `CORSIA_LOG_CAPTURE_TEST_ENABLE=1` opts an isolated test
instance in.

`log_read` is listed for every thread, because the tool index is shared.
Execution refuses a thread that `get_coordination_scopes` does not name. It filters
by UTC window, minimum level, logger prefix and case-insensitive text, and
returns records newest first with a 16 KiB response bound and a paging note.
This path adds no redaction layer: secrets must not be logged at the source.
The registry's existing credential sanitizer still applies to every tool
result. Project binding, keyed intake, cursors and the public layer are later
work.

## Review authority

The reviewer builds its request from the current diff plus the mapped approved
PRD contract. GitHub trigger, review, comment, and check handling lives under
`backend/reviewer/`.

Author procedure, revised-head holds, and merge gates live in the project
`sdlc-process` skill. The PR template captures the current scope fence,
verification, review-head attribution, and rollout risk without recreating a
second contract system.

## Historical material

Use git history for deeper provenance. New rationale belongs in the current
PRD/design and the code/tests that implement it.
