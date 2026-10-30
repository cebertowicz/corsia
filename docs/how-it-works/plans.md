# Plans

A plan is a thread's structured record of what it has committed to do, what it is waiting on and what it has asked the person. This page covers the task lifecycle, waits, decisions and liveness, and what can go wrong.

## What it is

- **Plan:** one per thread. It is either active or paused.
- **Node:** one unit in a plan. A task is a node with acceptance criteria: the observable evidence that has to exist before it counts as done.
- **Decision:** a question for the person, recorded on the plan with options and a recommendation.
- **Wait:** a task set aside until a time, an event, another task or a milestone.
- **Hold:** a recorded reason a task cannot advance, with the party who can end it.

## Why it exists

Chat history is a poor record of commitments. A long conversation buries what was promised, an agent that stops mid-task leaves no trace of what remains, and "done" is whatever the last message claimed. A plan makes the commitment a separate, checkable object. Whether a task is accepted is a recorded state change, which lets [Measuring work](measuring-work.md) count eligible outcomes without reading transcripts.

## How it works

### Task lifecycle

A task moves through a fixed set of states, and some are terminal (`backend/plans/node_states.py`).

```mermaid
stateDiagram-v2
    [*] --> pending
    pending --> active
    pending --> review_requested: delegated return
    pending --> failed
    pending --> closed
    active --> review_requested
    active --> failed
    active --> closed
    review_requested --> accepted
    review_requested --> accepted_with_followup
    review_requested --> active: sent back
    review_requested --> failed
    review_requested --> closed
```

- `pending`, `active`: not yet started, or being worked.
- `review_requested`: the work is handed back with evidence, awaiting acceptance.
- `accepted`, `accepted_with_followup`: verified complete; the second records a required follow-up.
- `failed`: the objective could not be met, kept on the plan as that record.
- `closed`: no longer wanted. It stays on the plan but leaves the work.

`accepted`, `accepted_with_followup`, `failed` and `closed` have no way out. A task cannot be reopened; a new task is added instead. A reviewer sending work back returns it to `active`. A pending task whose delegated worker already returned can move straight to `review_requested`; only the delegation harvest writes that edge.

### Delegated work

A task handed to a worker stays on the parent's plan and is linked to the worker's plan. The worker returns evidence and the parent accepts or rejects it; see [Agents and threads](agents-and-threads.md).

### Pause and wait

- **Pause is the person's.** Only the person pauses or resumes a plan. An agent that wants to come back to a task later schedules it instead (`backend/plans/pause_authority.py`, `backend/plans/scheduled_wait.py`).
- **Waits are watches.** A task can wait on a deadline, an event, a milestone or another task (`backend/registry/attention.py`).
- **Event waits have deadlines.** An event wait is armed with a deadline: 24 hours by default, at most 7 days. On expiry the system attempts to wake the owner with a note that the event did not arrive (`backend/plans/event_wait.py`, `backend/automation/continuation/sources/watch_expiry.py`). A scheduled task keeps whatever date the agent chose; a blocked wait refuses a date past 7 days.
- **Holds** record who can end a block: an external party, the system, another thread, or the person (`backend/plans/holds.py`).

### Decisions

A decision goes through draft, parent review, raised, then answered, or invalidated if it can no longer be answered (`backend/plans/decision_lifecycle.py`). It has two to four lettered options and a free-form "other", which must always be offered, plus a recommendation; it can ask for one pick or any subset (`backend/plans/decision_fields.py`). A decision belongs to the plan, not to the agent that asked it:

- If an asking thread is archived but the owning plan remains, the question stays and still lands on that plan.
- If the owning plan goes away, including when its owner thread is archived, or the task it came from is closed, the question is withdrawn as `invalidated` and not counted as a failure.

### Liveness

A thread with actionable plan work can receive a liveness wake. The initial idle interval is 180 seconds; subsequent idle intervals double while there is no structured progress. A wake delivered to an already running turn uses a 600-second interval instead (`backend/automation/plan_liveness.py`, `backend/automation/continuation/sources/liveness.py`). The wake is stored durably and checked against current plan state before delivery. A failed delivery marks it stalled; unchanged work does not automatically re-arm that stalled wake.

## What can go wrong

- **No plan, no outcome.** Work done without a plan task is not counted as an outcome and has no acceptance record.
- **Acceptance is only as good as the criteria.** The system records that criteria were stated and a task was accepted; it does not judge whether the criteria were the right ones.
- **A wait can end without its event.** The deadline prompts a wake saying the event did not arrive; delivery can fail, and the owner still has to act.
- **A paused plan stops agent-driven work.** Agents cannot unpause it. Scheduled wakes, connected events and worker completions are set aside in a digest; messages from a person or another agent, decision answers and restart continuations can still reach the thread (`backend/plans/pause_authority.py`).
- **Terminal states are final.** A mistaken `closed` or `failed` cannot be undone; add a new task.

## Where it lives

| Part | Path |
|---|---|
| Node states and legal transitions | `backend/plans/node_states.py` |
| Closing, delegation, decisions | `backend/plans/closure.py`, `backend/plans/delegation.py`, `backend/plans/decisions.py` |
| Pause and scheduled waits | `backend/plans/pause_authority.py`, `backend/plans/scheduled_wait.py`, `backend/plans/event_wait.py` |
| Decision lifecycle | `backend/plans/decision_lifecycle.py` |
| Liveness wakes | `backend/automation/plan_liveness.py` |
| Holds and decision fields | `backend/plans/holds.py`, `backend/plans/decision_fields.py` |
| Plan tool an agent calls | `backend/tools/plans.py` |
