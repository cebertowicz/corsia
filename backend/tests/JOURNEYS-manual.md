# JOURNEYS — the steps no automation here can honestly reach

Companion to the journey suite:

| File | Journeys |
|---|---|
| `backend/tests/test_user_journeys.py` | A1 — typed to answered: J1 cold open, J2 tools, J3 queue, J6 steer |
| `backend/tests/test_user_journeys_endings.py` | A2 — taking it back: J4 Stop (incl. J4 PAST, a Stop with both a steer and a queue behind it), J5 Cancel |
| `backend/tests/test_user_journeys_durability.py` | B — J7 exits, J8 repeat, J9 second surface, J10 crash and boot, J11 model switch (incl. the `428 checkpoint_required` downshift) |
| `backend/tests/journey_fixtures.py` | the shared fixture and read helpers (drives `tests/contract/matrix_runner.py`) |
| `frontend/src/lib/journeys/*.journey.test.ts` | the rendering half: the chip, the cancelled bubble, the tool spinner |

Everything in this file is a step from the journey spec that the automated
suite **does not** cover. It is written down so
the gap is a printed fact somebody has to read, rather than an omission nobody
notices — the same rule the turn-lifecycle matrix follows when it prints
`UNCOVERED` cells.

A step listed here is **not covered**. Do not count it as tested. Before a
cutover deploy is called done, walk it by hand and record the date.

---

## 1. Proven non-vacuous (evidence, not a gap)

A green suite that cannot fail is worse than no suite. Each cell below was
reddened by breaking the behaviour in the production source and re-running; each
breakage was reverted afterwards. A future edit that weakens one of these cells
should redo its breakage.

| # | Breakage applied to production source | Cells that went red |
|---|---|---|
| 1 | `ws/stop_control.py` — on Stop, cancel every entry in the thread inbox instead of only the running turn. | `test_j4_a_queued_message_survives_a_stop_and_delivers_itself` — the survivor lands `undelivered/cancelled_by_user`. This is the reported Stop defect, reproduced. |
| 2 | `infra/thread_inbox._cancel_in_transaction` — end a cancelled input without reserving a sequence (`sequence = None`). | `test_j5_a_cancelled_message_stays_visible_terminal_and_timestamped` — `seq is not None` fires; the cancelled message would vanish from the transcript. |
| 3 | `api/turns.py::get_active_turns` — drop the `t.status = 'active'` filter so ended turns still count as live. | `test_j1_3_to_1_5_one_send_lands_once_and_leaves_the_thread_idle_clean`, `test_j2_a_tool_turn_binds_its_result_and_leaves_nothing_running`, `test_j3_a_queued_input_holds_no_place_until_it_earns_one` — all fail on the A12 clause of `assert_idle_clean`, which is the phantom Stop button of USER-REPORTS #6b. |
| 4 | `frontend/src/lib/messageDelivery.ts::isQueuedAfterTurn` — return `true` for `delivery_intent === 'queue_after_turn'` regardless of delivery state. | All three cells of `queueToDelivered.journey.test.ts` — the chip stays on the composer after the message is in the transcript. This is the reported chip defect, reproduced. |
| 5 | `frontend/src/lib/messageDelivery.ts::blockedReason` — return the raw code instead of the copy. | Ten `renders <code> as words` cells in `cancelledInput.journey.test.ts` fail on the machine-code assertion. |
| 6 | `frontend/src/lib/components/UserMessageBubble.svelte` — print the timestamp for `undelivered` rows too, i.e. **fix** the open defect below. | The `it.fails` cell "shows the time it was written" goes red. The marker is live in both directions: it holds while the defect exists and demands its own deletion the moment it is fixed. |
| 7 | None — this one was not a probe. `reserve_transcript_position` was written by another agent while this suite was being built. | `test_j5_past_a_cancelled_message_never_jumps_the_one_it_waited_behind` went `XPASS(strict)`, i.e. red, on the fix landing. Failed against the unfixed code, passes against the fixed code, and the fix's author never saw the test. See §2. |

---

## 2. Open defects the suite documents rather than hides

**None right now.** Both entries this section carried have been fixed and their
markers deleted; §2b records what they were and what closed them. If you add a
marker, add its row here — a marker with no row is a defect nobody is counting.

Strict markers on purpose: when one of these is fixed the cell turns **red**,
which is the reminder to delete the marker.

## 2b. Closed — what the markers caught

| Was | Journey step | Defect, and what closed it |
|---|---|---|
| `it.fails` — `cancelledInput.journey.test.ts` "shows the time it was written" | J5 (P3) | `UserMessageBubble.svelte` printed `formatTime(msg.created_at)` only when `deliveryState === null`. A cancelled row is `undelivered` forever, so the last message the user sent carried no time at all. Closed by making the gate `deliveryStillMoving` — `undelivered` is an ENDING, and an ending has a time. |
| `xfail(strict)` — `test_j10_past_the_thread_answers_the_next_message_after_a_boot` | J10 PAST (A9) | **Found by this suite**, and it took TWO independent bugs, either of which alone wedged the thread. (1) `runnable_head_turn_fence_clause` fenced a thread on ANY turn row holding `write_intent_at` with no `write_outcome` — and a crash leaves exactly that on a turn boot then marks `interrupted`, while `record_write_outcome` only updates turns that are still `active`. The outcome could never be filled in, so the fence never lifted. (2) `enqueue_event_wake` stored the raw `sessions.model` string as the descriptor's `target_model_id` while every other admission stores the RESOLVED catalog id, so `select_compatible_prefix` dropped the head (`mock/test-model` vs `mock-test-model`) and promotion declined `terminal_prefix_unplannable` forever. Both are fixed; the recovery event now delivers and is answered, and the next user message is answered after it. |

### Closed while this suite was being written

`test_j5_past_a_cancelled_message_never_jumps_the_one_it_waited_behind` was
written as a strict xfail against the cancelled-input ordering defect (the
owner-reported "a cancelled message takes the wrong sequence"). It turned red —
`XPASS(strict)` — the moment `core.delivery_reactor.attempt_settlement.
reserve_transcript_position` landed and booked the older in-flight entry's place
before the cancellation drew one. The marker has been deleted and the cell is an
ordinary passing journey.

That red→green transition is the strongest non-vacuity evidence in this file:
the cell failed against the unfixed code and passed against the fixed code,
with the fix written by someone who never saw the test.

---

## 3. Journey steps already owned by an existing test

Covered, but not here — listed so nobody adds a second copy and so a reader
looking for the step can find it.

| Spec step | Owner |
|---|---|
| J1.2 / A4 — a typed draft survives a thread switch and a failed send, and does not contaminate the other thread | `frontend/src/lib/components/InputBar.test.ts` ("preserves a full submitted draft through a thread-switch failure…") |
| A2 — the composer button is a pure function of turn state (Send / Stop / Stopping) | `frontend/src/lib/components/InputBar.test.ts` ("acknowledges a Stop while its turn is still live…") |
| A6 (render side) — a queued input never renders among delivered rows; the pending region holds imminent delivery only | `frontend/src/lib/components/MessageList.pending-delivery.test.ts` |
| The queue chip's own markup, ordering and cancel control | `frontend/src/lib/components/InputBarQueuedInputs.test.ts` |
| Turn-lifecycle cells (plain, tool, stop, retry, queued-preemption, pressure, restart-recovery) as a merge gate | `backend/tests/contract/test_i10_lifecycle_matrix.py` |
| "A thread never wedges" as a cross-tranche property | `backend/tests/test_acceptance_no_wedge.py` |

## 4. Manual only — device and gesture

Automated tests drive synthetic events; none of these can be honestly claimed
without a hand and a real device.

- **Hold-to-queue on real touch (USER-REPORTS #1).** Press and hold Send on a
  physical iPhone during a running turn. The button must fill sky, haptics must
  fire at the arm threshold, and release must QUEUE (chip on the composer), not
  steer. Repeat with a hold that travels (a scroll) — that must NOT queue. The
  arm timing and `pointercancel` behaviour are Safari-specific; jsdom cannot see
  them.
- **`Ctrl`/`⌘+Enter` as the desktop equivalent of the hold.**
- **Mobile layout at 390px (A8).** Nothing scrolls the page body sideways: wide
  code blocks, tool diffs and tables scroll inside their own container. Check
  with a queue stack of 3+ chips and a long single-word message.
- **Two physical devices on one thread (P7, [2S]).** Phone and desktop on the
  same thread: queue from the phone, watch the desktop's chip appear and then
  leave. The backend half of this is automated (`test_j9_…`); the pixels are not.
- **Scroll-follow (J1.4).** Scroll up mid-stream: following stops and does not
  snap back on each token; scroll down: following resumes; the turn ending
  causes no jump.
- **Visual coherence.** Light and dark, reduced motion, the failed `!` marker
  against the accent bubble.

## 5. Manual only — real harnesses

The isolated backend patches exactly one seam (`llm.base.acompletion`). The
claude-code and codex transports spawn real CLI binaries, so every journey below
would make live, billed, non-deterministic provider calls. `HARNESS_UNCOVERED`
in `tests/contract/matrix_runner.py` records the same gap for the lifecycle
matrix; the journeys inherit it.

- Every journey in Part A and Part B, on **claude-code** and on **codex**.
- Steering divergence: Inject on native/codex vs Preempt on cc. Only the native
  path is driven here.
- Edit-and-resend on each harness **without an app restart** (USER-REPORTS #2),
  and on native, telemetry showing the warm path rather than a cold refill.

## 6. Manual only — real time

- **Warm checkpoint end to end (USER-REPORTS #5, #961).** Force a checkpoint on
  a large native thread and a codex thread; confirm a `warm_path` telemetry row
  appears and that post-checkpoint provider input tokens actually drop. No
  successful checkpoint has been observed in production since the fix deployed.
- **Overnight scheduling.** Observe background checkpoints and ordinary FIFO
  delivery through a real overnight interval; mock-clock tests do not prove
  sustained operation.
- **Checkpoint provider interruption.** Kill the codex bridge mid-checkpoint;
  confirm the checkpoint failure is visible and Stop remains responsive. An
  optional checkpoint failure must preserve a continuation that still fits.
  If a required checkpoint cannot be constructed, accepted input must remain
  queued with its original text, not fail or be retried by an expiry timer.
  Verify an actual checkpoint installation or effective context-cap change
  releases eligible input without resending anything possibly already written
  to the provider. The isolated delivery/release tests cover these state
  contracts; this real-provider interruption remains an unperformed manual
  acceptance step until its result is recorded.
- **Cost and usage accounting against a real bill.** The suite counts provider
  calls at the mock seam (A11) — it cannot see money.

---

## 7. How to add to this file

If a journey step cannot be automated, it goes here in the same sentence-form:
what the person does, and what they must observe. Do not write a test that
asserts something weaker than the step and call the step covered. The four
defects this suite exists to catch all escaped through exactly that door.
