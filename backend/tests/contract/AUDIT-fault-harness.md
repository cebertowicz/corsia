# Adversarial audit — the I2/I8 crash-convergence harness

**Scope.** `tests/contract/fault_harness.py` and
`tests/contract/test_i2_i8_crash_convergence.py`, audited against the real
startup sequence (`app_lifecycle.py`, `automation/restart_startup.py`) and the
real recovery machinery (`core/delivery_resolver.py`,
`infra/delivery_operation_recovery.py`, `core/context_cutover.py`).

**Method.** Code reading plus five executed experiments: a SQLite
`set_trace_callback` census of every real `COMMIT` a scenario issues; a probe of
which production modules the seam actually covers; a "total seam" that patches
every module alias of `get_db` so that the *complete* commit set becomes
crashable; a full scenario × kill-point matrix under that faithful seam; and a
direct execution of production's real recovery path
(`reconcile_delivery_recovery_state` → `resolve_thread_delivery`) against each
stranded state. Every numeric claim below is measured, not inferred.

**Bottom line.** The harness is the right idea and its one red is real. But its
crash model covers fewer commits than it claims, its kill-point count is wrong
and depends on test execution order, and its convergence predicate is far weaker
than invariants I2/I5 require. Under a faithful seam the same three scenarios
produce **two** unhealed states, not one — the second is inside the `stopped`
scenario, which the harness currently scores green.

---

## Verdict on question 6 — is the `delivered` kill@6 red trustworthy?

**Yes. Unreservedly. It is a real, unhealed durable state, and the harness
under-states how bad it is.**

The obvious way it could have been an artifact is real: `run_boot_pass()`
(`fault_harness.py:118-152`) calls `resolve_delivery_operation(thread_id)` but
**not** `reconcile_delivery_recovery_state(thread_id)`. Production never calls
the former alone — `core/delivery_resolver.py:114-158` (`_resolve_thread_delivery_step`)
runs `reconcile_delivery_recovery_state` first (line 122) and only then
`resolve_delivery_operation` (line 139). `resolve_delivery_operation` returns
early for exactly this phase (`infra/delivery_operation.py:264`), so the
omitted step is precisely the one that is supposed to settle a `successor_admitted`
operation whose successor turn is terminal
(`infra/delivery_operation_recovery.py:206-215`). The production log line the
test emits says so out loud: *"terminal settlement left successor operation …
for resolver reconciliation"* (`infra/delivery_terminal_settlement.py:67-72`).

I ran the omitted step against the stranded state. It does not heal it — **it
raises**:

```
reconcile_delivery_recovery_state RAISED DeliveryOperationConflictError:
    pre-write successor unexpectedly owns provider write intent
resolve_thread_delivery  RAISED DeliveryOperationConflictError: (same)
   delivery resolver retry 1/2 …
   delivery resolver retry 2/2 …
   delivery resolver exhausted for delivered-k6 after 3 attempts
violations after the REAL recovery path:
   ['operation … (successor_admitted) outlives its terminal successor']
```

The full production path is a dead end, not a delay:
`resolve_thread_delivery` retries twice (`core/delivery_resolver.py:210-256`),
then calls `fail_delivery_resolution`, whose **first act is to call the same
`reconcile_delivery_recovery_state` that just failed**
(`infra/delivery_operation_recovery.py:313`) — so the "expose exhaustion
durably" escape hatch raises too and the resolver task dies with an unhandled
exception (`core/delivery_resolver.py:292-294` logs it and stops). The user's
input stays `delivery_state='queued'` with a claimed attempt and an armed
provider write fence, forever, across any number of restarts.

Nothing about this is harness-specific: the same durable state, healed by the
same (failing) code, is reached whether recovery is invoked the harness's way or
production's way. **Keep the red. Treat it as Phase 1 demolition evidence.** See
production bug **PB1** below for why this state is not exotic but modal.

---

## Findings, by severity

### F1 — CRITICAL: the crash seam does not intercept every commit; the kill-point count is wrong, order-dependent, and one scenario is a false green

`install_crash_seam` (`fault_harness.py:75-83`) patches the *module attribute*
`infra.database.get_db`. But ~60 production modules bind that function into their
own namespace at import time — `core/turn_core.py:12`, `core/turns_util.py:10`,
`infra/queries.py:10`, `infra/thread_inbox.py:16`, `infra/delivery_operation.py:15`,
`infra/context_cutover.py:21`, `core/context_cutover.py:55` (which then snapshots
it again into `_cutover_connection_factory` at line 77), and so on. Patching the
module attribute cannot reach any of them. Two failure modes follow, and both
are live:

* A module imported **before** the seam keeps the pristine `get_db`; its
  `commit()` calls are never counted and never crashable.
* A module imported **during** a seam window binds *that window's* closure
  permanently, so its commits are charged to a stale, long-dead budget.

Measured (`tests/contract` full suite, seam probe plugin):
`infra.queries` and `infra.context_cutover` hold the pristine `get_db` for the
entire session. Running the file alone, 15 modules end up pinned to the first
dry run's budget: `core.turn_core`, `core.turns_util`, `core.context_cutover`,
`core.portable_checkpoint`, `core.turns_steps`, `infra.thread_inbox`,
`infra.delivery_operation`, `infra.queries`, `infra.context_cutover`,
`infra.context_receipts`, `infra.llm_usage`, `infra.parent_completion`,
`infra.parent_completion_delivery`, `infra.analytics_db`, `core.token_estimation`.

Most delivery writes survive this because they go through
`write_transaction` (`infra/database.py:152-213`), which resolves `get_db` from
`infra.database`'s own namespace and is therefore patched. The exception is the
single most important boundary in the flow: **`persist_turn_terminal` commits on
its own module-level `get_db` (`core/turn_core.py:12`, commit at line 431), so
the turn-terminal transaction is never a kill point.**

Measured consequence, reproducing the test's exact order (dry run, then one
replay per prefix):

| scenario | commits the harness counts | real commit boundaries | kill points that actually crash |
|---|---|---|---|
| `delivered` | 8 | 8 | 7 (kill@7 does **not** crash) |
| `stopped` | **6** | 7 | 6 |
| `pressure_paused` | **3** | 4 | 3 |

`stopped` and `pressure_paused` lose a boundary outright because by the time
their dry runs execute, `core.turn_core` is already pinned to the *`delivered`*
dry-run budget. So `total` — the denominator printed in the failure message —
is a function of which scenario pytest happened to run first.

**The lost boundary is the one that wedges.** Under a faithful seam that patches
every alias, `stopped` kill@6 (die at `persist_turn_terminal`) produces exactly
the F-verdict state and exactly the same production failure:

```
=== stopped: 7 real commit boundaries ===
  kill@6: harness=['operation … (successor_admitted) outlives its terminal
                   successor on stopped-k6']
          real_err=['DeliveryOperationConflictError: pre-write successor
                     unexpectedly owns provider write intent']
```

The harness reports `stopped` as converging at every kill point. It does not.
That is a false green produced by the seam, in the scenario the STATE file cites
as covering the #1018 shape.

**Recommendation.** Do not patch a name — patch the *thing*. Wrap the connection
factory at its single source: have the seam replace `infra.database._make_connection`
(or set the budget on a module-level hook that `get_db` itself consults), so
every caller, every alias and every db-pool worker thread inherits it regardless
of import order. Then assert the invariant the harness silently depends on:
after the dry run, `total` must equal the number of `COMMIT` statements observed
by `sqlite3.Connection.set_trace_callback`, and every replay at `kill_point < total`
must end in `SimulatedCrash` (assert `isinstance(run.scenario_error, SimulatedCrash)`
— today it is recorded at `fault_harness.py:200-203` and never checked).

---

### F2 — CRITICAL: `convergence_violations()` cannot see a pause with no exit, and the `pressure_paused` scenario proves it

`convergence_violations()` (`fault_harness.py:155-190`) has exactly three
predicates: no `active_turns.status='active'`; no `messages.delivery_state='queued'`
lacking a `thread_inbox` row; no non-terminal `thread_delivery_operations` row
whose `successor_turn_id` is terminal. There is **no predicate over
`context_cutover_state` at all**, and none over `context_cutover_inputs`.

Measured: the `pressure_paused` scenario — at kill@3, kill@4 and on a clean
completion — leaves

```
cutover: {'session_id': 'pp-k4', 'status': 'safe_paused',
          'error_code': 'tool_result_fit_unproved',
          'reason': 'tool result cannot safely continue on the live provider
                     context', 'retry_at': None, 'next_input_queue_order': 1}
convergence_violations: []
```

`safe_paused` is the state that required manual `cancel` API calls against
production this week. Real boot does not clear it either: it is excluded from
the resolver's candidate page (`infra/delivery_recovery_candidates.py:51-52`,
via `OPEN_CUTOVER_STATUSES`) and it is not in `recover_pending_context_cutovers`'s
resume set (`core/context_cutover.py:1786-1801`). The harness scores the exact
production wedge green, in the scenario named after it.

Wedge classes the checker misses, enumerated (answering question 4):

1. **A pause with no exit.** Any `context_cutover_state` status
   (`safe_paused`, `provider_paused`, `retry_wait`, `pending_checkpoint`,
   `committing`, `failed`) surviving the boot pass. Not checked. **Missed.**
2. **An input queued but invisible.** Only one direction is checked. Missed:
   (a) a `thread_inbox` row whose message is `delivered`/`undelivered` — a
   tombstone head that blocks the whole FIFO behind it; (b) an input parked in
   `context_cutover_inputs` (the second queue I1 exists to kill) — that table is
   never read by the checker *or* by the digest; (c) a message durably in
   `delivering`, or `undelivered` with a `delivery_error` and no exit.
3. **A head parked forever in the future.** `messages.next_attempt_at` is never
   examined. `_park_unprogressed_due_heads` writes now+300s
   (`core/delivery_resolver.py:330-386`) and `fail_delivery_resolution` rewrites
   schedules; a head parked at year 2030 satisfies every predicate. **Missed.**
4. **A stale claim.** `thread_inbox.attempt_id`/`claimed_at` set with no live
   owner is never checked; claims do not expire. The kill@6 residue includes
   exactly this and is only caught incidentally, via the operation predicate.
5. **An operation outliving its terminal successor.** Checked — but only for
   `successor_turn_id`. An operation stuck in `waiting_predecessor` whose
   `predecessor_turn_id` is terminal or absent, and an operation in a mutable
   phase with `successor_turn_id IS NULL` and no FIFO head at all (the state
   `_settle_headless_operation` exists to repair,
   `infra/delivery_operation_recovery.py:71-136`), both evaluate to
   `successor_status is None` and pass. **Missed.**
6. **A durably-active turn with no owner.** The predicate exists but is vacuous:
   `run_boot_pass` itself calls `mark_orphaned_turns`, which terminalizes *every*
   active turn unconditionally (`core/turns_util.py:51-98`). The predicate can
   only fail if that function throws. It proves nothing about ownership.

**Recommendation.** The predicate list is the contract; grow it to match the
sentences in the plan. Minimum additions: (i) no open `context_cutover_state`
row without both a resume and a cancel path available and a user-visible reason
(I5's exit parity, machine-checked); (ii) `context_cutover_inputs` must be empty
after convergence (I1); (iii) every `thread_inbox` row must reference a
`queued`, error-free, unsequenced message; (iv) every queued head must be
*schedulable* — `next_attempt_at` within a bounded horizon and its
`delivery_revision` matching its operation; (v) no `attempt_id` without a live
owner; (vi) the operation predicate extended to predecessors and to headless
mutable operations. Each should carry its own failure sentence so the matrix
stays a worklist.

---

### F3 — HIGH: `run_boot_pass()` is not a model of real startup — it omits production's actual repair step and invents a stronger one

Real boot, in order (`app_lifecycle.py:96-255`, `automation/restart_startup.py:128-146`):

```
init_db()                                   # schema + ~90 forward migrations
recover_write_intents()                     # registry/durable_writes.py:889
recover_interrupted_relocations()
recover_startup_continuity():
    repair_orphaned_tool_calls()
    register_orphaned_turn_wakes()          # automation/restart_wakes.py:249
    mark_orphaned_turns()
    quarantine_stranded_tool_results_fleet()
    close_out_prior_self_restart()
    enqueue_restart_wakes() ; replay_held_webhooks() ; complete_restart_operation()
recover_delegated_work_after_discovery()
configure_context_cutover_generator(generate_portable_checkpoint)
recover_pending_context_cutovers()          # core/context_cutover.py:1740
delivery_recovery_sweep_loop()  ->  recover_delivery_operations()
    -> read_delivery_recovery_candidates()  # infra/delivery_recovery_candidates.py:82
    -> reconcile_delivery_recovery_state() ; resolve_delivery_operation()
    -> _park_unprogressed_due_heads()
```

`run_boot_pass()` runs: `repair_orphaned_tool_calls`, `mark_orphaned_turns`,
`quarantine_stranded_tool_results_fleet`, then `resolve_delivery_operation` for
every thread with a `thread_inbox` row, then `reconcile_context_cutover_recovery`
for cutovers `NOT IN ('completed','failed','cancelled')`.

**Omissions that hide failures:**

* `reconcile_delivery_recovery_state` — production's *only* repair for
  operations that lost their runtime owner. Its absence is why the kill@6
  failure message names a symptom rather than the raising code (see the
  verdict). Everything the resolver can repair — headless operations, prewrite
  claim release, unknown-write requeue — is untested.
* `init_db()` — I8's text explicitly covers "boot migrates durable rows forward
  once", and not one migration runs. `_apply_delivery_contract_cutover`,
  `_rebuild_delivery_operation_phases` (`infra/database_migrations.py:5103`),
  `_rebuild_parent_completion_status`, `_requeue_quarantined_messages` are all
  repair code that boot executes and the harness never exercises.
* `register_orphaned_turn_wakes` — real boot reconciles every active turn into a
  wake registration *before* `mark_orphaned_turns` terminalizes it
  (`automation/restart_wakes.py:249-250`). Skipping it means the harness's boot
  terminalizes orphans with no resumption path, and any wedge inside the wake
  machinery is invisible.
* `recover_write_intents`, `recover_interrupted_relocations`,
  `close_out_prior_self_restart`, `enqueue_restart_wakes`, `replay_held_webhooks`,
  `recover_delegated_work_after_discovery` — all durable-state repairs, none modeled.
* `recover_pending_context_cutovers` — the harness calls a *different, lower-level*
  function with a *different* filter. Real boot includes `failed` rows (which the
  harness excludes) and any thread with `context_cutover_inputs` rows (which the
  harness cannot see at all), and then runs the resume half
  (`start_context_cutover_owner`) that the harness never runs. It also never runs
  `configure_context_cutover_generator`, so the subsystem is in a different
  configuration than at boot.

**Inventions that could manufacture a false green:**

* `run_boot_pass` resolves **every** thread with a FIFO row, unconditionally.
  Production's candidate page (`infra/delivery_recovery_candidates.py:44-57,
  82-130`) excludes threads with an open cutover, archived threads, threads with
  an active turn, and heads whose `next_attempt_at` is in the future. So the
  harness "heals" threads production deliberately never touches — precisely the
  parked-head and cutover-owned wedges from F2.
* Production's first sweep is not immediate: `delivery_recovery_sweep_loop`
  sleeps `sweep_delay_seconds(due_in, 60)` *before* its first recovery pass
  (`core/delivery_resolver.py:465-471`), i.e. up to 60s when nothing is due.
  "One boot pass" in the harness is instantaneous and synchronous; in production
  it is an asynchronous sweep with backoff. If I8 means "no manual repair", say
  so and model the sweep; if it means "before serving", the sweep is in the wrong
  place and that is itself a Phase 1 finding.

**Recommendation.** Compose `run_boot_pass` out of the *same* callables real boot
uses, in the same order, with a comment naming each source line — ideally by
extracting the production sequence into one function that both `app_lifecycle`
and the harness call, so the two cannot drift. At minimum: add
`reconcile_delivery_recovery_state`, drive threads through
`read_delivery_recovery_candidates` rather than a raw `SELECT DISTINCT`, and
call `recover_pending_context_cutovers`. Where a step genuinely cannot run in
the harness, state the exclusion in the docstring instead of quietly dropping it.

---

### F4 — HIGH: the fixpoint digest is too narrow to detect most durable change

`delivery_state_digest()` (`fault_harness.py:94-115`) hashes 5 tables and about
20 columns. The schema those tables actually carry:

| table | columns hashed | columns present |
|---|---|---|
| `messages` | 7 | 13 |
| `thread_inbox` | 6 | 8 |
| `thread_delivery_operations` | 3 | 18 |
| `active_turns` | 3 | 9 |
| `context_cutover_state` | 3 | 42 |

Concretely invisible to the fixpoint check:

* `messages.metadata` — where the tool-result binding state lives. A boot pass
  that re-quarantines the same rows on every boot
  (`quarantine_stranded_tool_results_fleet`, `core/turns_util.py:109-159`) is
  scored as a fixpoint.
* `messages.delivery_revision` — the entire retry fence. A boot that bumps
  revisions forever (`_advance_retained_head_revision`,
  `infra/delivery_operation_recovery.py:161-177`) reads as stable.
* `thread_delivery_operations.successor_turn_id` / `predecessor_turn_id` /
  `head_message_id` / `head_delivery_revision` / `terminal_at` /
  `historical_recovery_*` — a boot that retires one operation and creates a
  replacement with a fresh `operation_id` *is* visible (the id is hashed), but
  any rebinding within an operation is not.
* `context_cutover_state.retry_at`, `provider_attempt_state`,
  `next_input_queue_order`, `staged_*` — 39 of 42 columns. A cutover looping
  through provider attempts on every boot is a fixpoint.
* Whole tables never read: **`context_cutover_inputs`** (the second queue),
  `message_delivery_contracts`, `thread_parent_completions`,
  `portable_checkpoint_state`, `durable_write_intents`, the restart-wake tables.

**Recommendation.** Stop enumerating columns by hand. Derive the digest from
`PRAGMA table_info` over an explicit table set (so a new column is included
automatically and a dropped table fails loudly), and put every table the
delivery contract names in that set. Excluding a column should require naming
it, not forgetting it.

---

### F5 — MEDIUM: `SimulatedCrash(BaseException)` is absorbable, and the harness absorbs its own collateral

The docstring at `fault_harness.py:26-31` claims "no production tolerance net can
absorb it". That is not true of this codebase. An AST scan finds 13
`except BaseException`/bare handlers with no top-level re-raise:

```
infra/db_async.py:43            core/workspace_scaffolding.py:395
ws/turn.py:341                  ws/stop_control.py:168,180,192,203
subagents/cc_runtime_lifecycle.py:676   subagents/cc_runtime_resume.py:638
subagents/codex_inbox_batch.py:316      subagents/codex_runtime.py:2607
subagents/cc_runtime.py:1659            events/connectors/github_webhook.py:59
```

`infra/db_async.py:40-48` is the material one: **every** production DB write goes
through `run_in_db`, and its cancellation branch does `except BaseException: pass`
to consume the worker's failure. A `SimulatedCrash` raised inside a db-pool
worker whose caller was cancelled is swallowed silently. `ws/turn.py:341`
converts a `BaseException` into a future result. `ws/stop_control.py:168-203`
defers the failure and keeps running four more cleanup steps *after* the
simulated death before re-raising — a dead process does not do that.

None of these bite today only because the scenarios call `infra`/`core`
functions directly on the main thread and never traverse `ws/`, `api/`, or
`run_in_db`. They will bite the moment the scenarios become faithful (F6).

The harness also absorbs its own: `replay_under_budget` catches
`except BaseException as collateral` (`fault_harness.py:202-203`), stores it in
`run.scenario_error`, and **never asserts on it**. A scenario that dies of a real
production defect — a `DeliveryOperationConflictError`, a `TypeError`, an
import error — is scored identically to a clean simulated death. Combined with
F1 (a replay that never crashes at all), the harness has two independent ways to
report "converged" without having tested anything.

**Recommendation.** Assert the scenario error: it must be `SimulatedCrash` for
every `kill_point < total`, and anything else is a finding with its own message.
Add a contract lint (I9 already has the machinery in `conftest.iter_backend_sources`)
banning `except BaseException` without an unconditional re-raise in the delivery
layer. And note in the docstring that the in-process model cannot stop execution
the way SIGKILL does — the crash only stops the *current call stack*.

---

### F6 — MEDIUM: scenario fidelity — raw SQL, no real send path, no concurrency, no second queue

* `_scenario_pressure_paused` (`test_i2_i8_crash_convergence.py:134-167`) writes
  `active_turns` and `messages` rows with raw `INSERT`, bypassing
  `persist_turn_start`, `insert_message` and the sequence allocator. Observable
  artifact: the boot pass's system-interrupt message lands with `seq=1` in a
  session that already has a `seq=1` user message. Hard rule 4 says prefer real
  production writers; the stranded states this scenario generates are not proven
  reachable by any production writer.
* No scenario goes through `infra/send_service.py` / `ws/turn.py`, so the real
  ordering (admission transaction, then the separate provider-I/O fence
  transaction at `infra/send_service.py:3128-3175`, then settlement) is
  hand-re-implemented in the test rather than exercised. It happens to match
  today (verified against `infra/send_admission.py:280-408`) — but nothing keeps
  it matching.
* Nothing runs through `run_in_db`, so the 8-worker pool, its per-thread
  connections, and interleaving between two threads' writes are entirely
  unmodeled. `CrashBudget` (`fault_harness.py:34-44`) is not thread-safe
  (`self.commits += 1`), so it could not model them without a lock anyway.
* No scenario produces a `context_cutover_inputs` row, a second queued input
  behind the head, a preempting `apply_now` input, a retry, or a checkpoint
  fold — so the queue-ordering half of I2 ("pauses into a FIFO queue") is
  untested.

**Recommendation.** Build scenarios from the real entry points (the isolated
`e2e_app` env and mock provider already exist and the STATE file plans a matrix
runner over them). Where the entry point is async, drive it through `run_in_db`
so the pool is in the picture; add a lock to `CrashBudget` first.

---

### F7 — LOW: the uncrashed terminal state is never judged, and violations are read after two boot passes

* `for kill_point in range(total)` (`test_i2_i8_crash_convergence.py:191`)
  excludes `total`, so the state the scenario reaches with no crash is never
  checked. That state is not automatically healthy — it is where a leaked
  settlement or an unsettled operation would live. (Measured under a faithful
  seam it is currently clean for all three scenarios, so this is latent, not
  active.)
* `replay_under_budget` runs `run_boot_pass()` twice before calling
  `convergence_violations()` (`fault_harness.py:206-215`). The fixpoint check
  does catch a second pass that changes state, so this is not unsound — but the
  violations that get reported describe a state one boot never produces. Read
  the violations after the first pass, then run the second only for the fixpoint
  comparison.
* `convergence_violations()` is called outside the `try` that captures
  `boot_error`, and `delivery_state_digest()` hard-codes tables Phase 1 will
  drop (`thread_delivery_operations`, `context_cutover_state`). When they go, the
  suite fails with a bare `OperationalError` instead of a contract sentence.

---

### F8 — LOW: harness hygiene

`_fresh_db` (`test_i2_i8_crash_convergence.py:51-71`) mutates
`infra.config.DB_PATH` and `infra.database.DB_PATH` process-wide and never
restores them, and rebinds `db_mod._thread_local` without a teardown. It is
correct for this file today, but it is a session-global side effect in a suite
that other agents are actively adding files to; a fixture with teardown would be
cheap.

---

## Answers to the specific questions

**Q1 — is the crash model faithful? Which durable writes land without a
`commit()` through `infra.database.get_db`?**

Ranked by how much they matter here:

1. **Module-level `get_db` aliases** — the dominant hole, ~60 modules, quantified
   in F1. `persist_turn_terminal` is the casualty that changes results.
2. **`core/context_cutover.py:77`**, `_cutover_connection_factory = get_db` — the
   whole cutover subsystem takes a snapshot of the factory at import time.
3. **DDL runs in autocommit.** SQLite's legacy transaction control only opens an
   implicit transaction for DML; every `CREATE`/`ALTER`/`DROP` in
   `infra/database_migrations.py` is durable with no `commit()` call at all.
4. **`executescript` commits implicitly** before running —
   `infra/database.py:288` and eight sites in `infra/database_migrations.py`.
   Since `run_boot_pass` skips `init_db` entirely (F3) this is currently moot,
   and becomes a first-order blind spot the moment migrations are modeled.
5. **`sqlite3.connect` direct** — `infra/recovery.py:63,81` (corruption
   auto-recovery: copies and rebuilds the database file), and
   `infra/database.py:127-132` where `_make_connection` commits its PRAGMAs on
   the raw connection before any wrapper exists.
6. **The `db_async` worker pool** (`infra/db_async.py:13`, 8 threads). Each
   worker has its own thread-local connection. The seam would reach them if it
   patched the factory, but `CrashBudget` is unsynchronized, so the commit
   numbering would be nondeterministic under real concurrency.
7. **Savepoints.** Nested `write_transaction(db=conn)` frames issue
   `SAVEPOINT`/`RELEASE`, not `COMMIT` (`infra/database.py:192-202`). This is
   *correct* — a savepoint release is not a durability boundary — but it means
   kill points are transaction-granular, so all work inside one outer
   transaction collapses to a single crash point. Worth stating in the docstring
   so nobody reads "every durable write" as "every statement".
8. **`with connection:` context-manager commits** bypass `.commit()` entirely in
   `sqlite3`. Verified: production has zero such sites today
   (`_NoOpClose.__enter__/__exit__` at `infra/database.py:118-122` would delegate
   past the proxy, and `_CrashingConnection` defines no `__enter__` at all, so a
   new one would fail loudly rather than silently — acceptable).
9. **WAL** is not itself a hole: crash-at-commit means the frame is never
   written. But `_plain_db()` (`fault_harness.py:86-91`) opens a second
   connection while the pooled one may still hold state, and the comment "The
   dead process holds nothing" (`fault_harness.py:204`) is an assumption, not a
   check. I measured `in_transaction` on the pooled connection after every kill
   point in all three scenarios: `False` everywhere, because production paths
   roll back in `except BaseException` / `_NoOpClose.close()`. Not currently
   violated — but `core/turns_util.py:96-97` (`db.commit(); db.close()` with no
   `try/finally`) is inside the boot pass and would leak an open write
   transaction if it ever raised, and that transaction's rows would then be
   visible to — and committable by — the next production call on the same pooled
   connection. Assert `not conn.in_transaction` after `monkeypatch.undo()`
   instead of asserting it in prose.

**Q2 — does `SimulatedCrash` escape production tolerance nets?** On the paths
these three scenarios traverse, yes: every handler re-raises
(`infra/database.py:197,207`, `infra/send_admission.py:83`,
`infra/context_cutover.py:372`, `core/turn_core.py:612,677`,
`core/thread_lifecycle.py:487`). Codebase-wide, no: 13 handlers swallow, and
`infra/db_async.py:43` sits under every production DB write. See F5.

**Q3 — is `run_boot_pass()` honest?** No. See F3 for the full diff, the
omissions that hide failures (chiefly `reconcile_delivery_recovery_state` and
`init_db`), and the inventions that could manufacture false greens (resolving
threads production's candidate filter excludes).

**Q4 — are the convergence predicates strong enough?** No. See F2 for the six
wedge classes, with `safe_paused` demonstrated passing.

**Q5 — is the fixpoint check meaningful?** Partially. It is real (it would catch
a boot pass that needs two runs), but it hashes ~20 of ~90 relevant columns and
zero of the second-queue table. See F4.

**Q6 — is the kill@6 red trustworthy?** Yes. See the verdict section.

---

## Production bugs found (reported, not fixed — Phase 0 is contract-only)

**PB1 — a restart during any native direct send permanently wedges the thread.**
`infra/send_admission.py:380-406` admits the successor turn (phase
`successor_admitted`) in one transaction; `infra/send_service.py:3128-3175`
arms the provider write fence in a second. Nothing advances the phase to
`provider_write_started` for native sends — `resolve_delivery_operation` returns
early for `successor_admitted` (`infra/delivery_operation.py:264`) and the only
production caller that settles to `provider_write_started` is the Codex batch
path (`subagents/codex_inbox_batch.py:290`). So for the entire duration of every
native provider call the durable state is `successor_admitted` + armed fence. If
the process dies there, boot's `mark_orphaned_turns` terminalizes the turn as
`interrupted`, and `record_delivery_successor_terminal` then hits
`raise DeliveryOperationConflictError("pre-write successor unexpectedly owns
provider write intent")` (`infra/delivery_operation_transitions.py:216-220`).
The explicit-Stop fast path immediately above it (lines 128-152) is the *only*
branch that handles `successor_admitted` + armed fence, and it requires
`user_cancelled=True` and `status='cancelled'` — which a restart-marked
`interrupted` turn can never be. Result: input stuck `queued`, claim never
released, no user-visible reason, unhealable by any number of restarts. This is
the wedge behind both red kill points.

**PB2 — the resolver's exhaustion escape hatch re-enters the code that
exhausted it.** `fail_delivery_resolution` exists to "expose an exhausted
resolver failure durably", and its first statement is
`reconcile_delivery_recovery_state(thread_id, db=connection)`
(`infra/delivery_operation_recovery.py:313`). Whenever reconcile is the failing
step — as in PB1 — the escape hatch raises too, the resolver task dies with an
unhandled exception (`core/delivery_resolver.py:292-294`), and nothing durable
records why. Any wedge in reconcile is therefore silent by construction.

**PB3 — `safe_paused` has no automatic exit.** It is excluded from the delivery
recovery candidate page (`infra/delivery_recovery_candidates.py:51-52` via
`OPEN_CUTOVER_STATUSES`) and absent from `recover_pending_context_cutovers`'s
resume set (`core/context_cutover.py:1786-1801`), so boot neither delivers nor
cancels it. This matches the production incident that required manual `cancel`
calls. Note also the inconsistency that `reconcile_delivery_recovery_state`
guards on only `{'pending_checkpoint','committing'}`
(`infra/delivery_operation_recovery.py:47-53`) while the candidate page guards
on the full open set.

**PB4 (minor) — `core/turns_util.py:96-97`** commits and closes with no
`try/finally`; an exception between them leaks an open write transaction on a
*pooled* connection, which the next caller on that thread inherits.

---

## Reproduction

```bash
cd /Users/alice/.worktrees/collapse-phase0/backend
CORSIA_CONTRACT_SUITE=1 python -m pytest tests/contract/test_i2_i8_crash_convergence.py -q
```

The faithful-seam experiment is a ~40-line wrapper: patch
`infra.database.get_db` *and* every `sys.modules` entry whose `get_db` is the
pristine function, run the scenario under a `CrashBudget`, then restore both the
listed modules and any module imported inside the window (they bind the crashing
closure at import time). Under it, `stopped` has 7 commit boundaries rather than
6 and kill@6 reproduces the F-verdict wedge. Ground truth for the commit count
comes from `sqlite3.Connection.set_trace_callback` on the pooled connection,
counting statements beginning with `COMMIT`; it agrees with the faithful seam
(8 / 7 / 4) and disagrees with the harness (8 / 6 / 3).
