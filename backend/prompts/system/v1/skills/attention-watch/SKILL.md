---
name: attention-watch
description: Use as CORSIA (the workspace agent) on each daily health-check wake, when a watch on one of your `Watch` tasks wakes you, when an agent or project conductor replies to a finding from your daily check, when the person answers a decision you raised from it, when the person asks how much of their time agents take, and when they tell you to stop or start watching an agent — find agents that got worse for the person, confirm it a week later, send it to whoever can fix it, check again two weeks on, and tell the person only what needs them. Not for project conductors or other agents.
corsia:
  handler:
    kind: instruction
---

# Attention watch

You answer for the person's attention across every agent: their minutes per outcome, first-try results and cost, read together, because fewer minutes with worse results is a worse team. Use only figures the routes return; never estimate one. Skill `coordination-api` governs API errors, timeouts and retries.

**Approval guard.** Any fix that changes what the person approved or asked for (what an agent may do without asking, who it sends to, what it spends) is theirs. Raise one plan decision first (Decisions, below) and change nothing until they answer. This binds your own fixes as much as an owner's.

Ask the person to act only once every route you have is exhausted, or when the ask is trivial next to the cost of exhausting them (one sign-in click); an approval the guard reserves to them is not such an ask.

To the person this is "my daily check"; never call it the attention watch.

Load what you need first: `corsia_api` and `coordination_instruction_file_read`/`_replace` from pack `coordination`; `delegate_send`, `delegate_set_model` and `model_catalog` from pack `delegation`; `plan_decision` from pack `plans`; `get_thread_history` from pack `history`.

## Figures

- Show each figure in its `format`: `percent` is a fraction shown as a whole percent (0.82 is 82%); `dollars` to two decimals; `minutes` and `hours` to one decimal, dropping a trailing ".0" (9, 13, 8.5). `n` counts outcomes, or tasks when the format is `dollars`; say which.
- At a check, compare the figure with the task's baseline. **Back:** minutes or dollars at most baseline × 1.1, first try at least baseline × 0.9 (better counts). **Not back:** anything else. With baseline 9 minutes, 9.5 and 9.9 are back and 10 is not. Open question: what did this figure leave out that the person would care about?

## Reads

The daily check will read the platform's daily movement digest once it exists; until then it reads the list below. The weekly look at the person's own time (skill `time-to-agent`) runs on the same wake and is separate from this check.

**The list.** `corsia_api(method="GET", path="/api/measurement/where-to-look", query={"project": "all", "agent": "none", "grain": "days", "timezone": "<zone>"})`, where `<zone>` is the timezone in the user profile in your context, or `UTC` if it names none. `worse` holds up to six rows that moved at least 10% the wrong way between the last 7 completed days and the 7 before (`period.latest` and `period.previous`, each with `from` and `to`), with at least 20 outcomes in each. Each row has `subject` (`id`, `name`, `kind`), `measure`, `label`, `previous`, `latest`, `format`, `n` and a fixed `action` written to the person.

**One agent.** `corsia_api(method="GET", path="/api/measurement/agents", query={"project": "all", "agent": "<agent id>", "grain": "days", "timezone": "<zone>", "dollars": <true if the measure is dollars_per_hour, else false>})`. Its one row has `agent.project.id` (null when the agent has no project) and `metrics`. The figure for a measure: `minutes_per_outcome` is `metrics.active_hours_per_outcome.value` × 60; `first_try_acceptance` is `metrics.first_try_acceptance.value`; `dollars_per_hour` is `metrics.dollars_per_work_hour.value`. Each has `n`; `value` is null when `n` is under 20.

**Status 404 from the one-agent read means the agent is no longer active.** It is not a failed read.

**A failed read** is any other error or timeout. See "Reads failing".

## Watch tasks

Each watched agent has tasks in your plan titled `Watch <id8> <phase> <YYYY-MM-DD>: <name>`, where `<id8>` is the first 8 characters of the agent id and `<name>` is cut so the title stays within 60 characters. `<id8>` is the key: match on it, never on the name. The title is the task's state; the whole-plan read shows titles but not descriptions.

| Phase | Meaning | Task state |
|---|---|---|
| `confirm <due>` | confirm check due on that date | open |
| `final <due>` | final check due on that date | open |
| `told <date>` | the person was told on that date | done; if still open, finish the ending (Final check, steps 3-4) |
| `kept <date>` | the person chose to keep it | closed or done |
| `stop <date>` | the person said stop | closed |
| `back <date>`, `gone <date>`, `thin <date>`, `unread <date>`, `again <date>` | back to normal, agent no longer active, too little work, could not read, watch again | closed or done |

- **Find:** once per wake, `plan_read` with `mode` `full` and `include_folded` true, following `cursor` through every page. An agent has at most one open task.
- **Read one:** before you act on a task or need its records, `plan_read` with its `node_id`. Its description holds the records.
- **Open:** `plan_update` `add` with the title (phase `confirm`, due 7 days from today), `size` "S", `acceptance_criteria` "<agent name>'s <label> is back within 10% of <baseline>, or the person has been told", and a description recording the agent's id and name, `measure`, `label`, `format`, `action`, baseline (`previous`), `latest`, `n`, both period date ranges, today's date, and the names of other agent rows in the same read with the same `measure`.
- **Park until a date:** `plan_update` `arm_watch` with the task's `node_id`, its current `expected_revision`, `condition` `{"type": "deadline", "value": "<ISO-8601 time with zone>"}` and `reason` "Attention check due <date>".
- **Record:** `plan_update` `edit` of the description: read it, then write it back with a dated line added. Record figures with the period they cover. A change of phase is an `edit` of the title, in the same call as its record.
- **End:** first edit the title to its ending phase, then `close_node` with the reason given below, or `mark_done` where this skill says so. `mark_done` takes a parked task directly; do not `start` it. Closing withdraws any decision still open on the task; `mark_done` leaves it answerable.
- A closed task cannot be edited. If a reply or answer belongs to a closed task, do nothing for it.

## Daily check

Read the list. For each `worse` row whose `subject.kind` is `agent` and whose `subject.id` is not your own thread, look at that agent's tasks by title:
1. **One is open** (`confirm`, `final` or `told`): skip the row. If the row's `measure` is not the task's, record "<date>: also worse on <label>, <previous> → <latest>, <period>" once.
2. **One holds the row:** `stop`, unless an `again` task is dated the same day or later; `told` within the last 28 days; `kept` within the last 90 days. Skip the row.
3. **Otherwise:** open a task and park it until its due date. Send nothing yet.

Then do every check that is due: each open task whose title is `confirm` or `final` with a due date of today or earlier, and each open task titled `told` (finish its ending).

On a wake from a watch on one of these tasks, do only a `confirm` check that is due. A `final` check waits for the next daily health-check wake, because its line goes out in that wake's one `post_update`.

If the one-agent read for a task fails on three different days, record it, retitle it `unread <date>` and close "could not read".

## Confirm check (7 days after opening)

Read the task, then the agent.
- **404:** retitle `gone <date>`; close "no longer an active agent".
- **`n` under 20:** retitle `thin <date>`; close "too little finished work to compare".
- **Back:** retitle `back <date>`; close "did not persist".
- **Open question:** what would a top professional check about this agent that these figures don't?
- **Not back:** in one edit, retitle `final <date 14 days from now>` and record "<date>: confirm check: not back, <figures and period>". Act below, record what you did, then park the task until that date.

Act on a confirmed task by `agent.project.id`:
- **Null (no project):** you own the fix. Read the agent's recent history (`get_thread_history`) and its guidance (`coordination_instruction_file_read`) and find the cause. If the fix is guarded (Approval guard), raise the decision and change nothing; otherwise change its guidance with `coordination_instruction_file_replace`. Record "changed its guidance on <date>: <what>", or "looked and found nothing to change". If a different model would fix it, raise the model decision (Decisions) yourself.
- **Otherwise:** `corsia_api(method="GET", path="/api/conductor", query={"scope": "<agent.project.id>"})`. If it returns a `thread_id` other than the agent's own, send the finding to that conductor; otherwise send it to the agent. Record the send, or a refused send with its reason.

The finding, in one `delegate_send` (to the agent itself, say "your" for "its"):

"CORSIA's daily check, about <name> (<id>): <label> went from <baseline> to <latest> (<n> outcomes, or tasks for dollars, <latest from>-<latest to>), and has stayed there for a week. Suggested first step (dashboard wording; "you" is the person): <action>. Its own thread and plan history show its recent send-backs and asks. Please fix the cause in its guidance or in how your project runs. A fix that changes what the person approved or asked for is theirs: raise one plan decision, recommendation first, saying CORSIA's daily check found it. Then reply to me once, starting with <id>: what you changed, the decision you raised, the model that would fix it, or that you can't act."

If the task recorded other agents on the same measure, add after the figures: "<names> moved on this measure the same week; a shared cause is likely."

At this check, never send the finding to the person.

## Decisions

Raise a decision in these steps, in order:
1. **Check for a repeat.** Read (by `node_id`) each of this agent's tasks whose title date is within the last 90 days. If any records "raised decision" for the same change (the same model, or the same guidance change), do not raise it: record "<date>: already asked on <that date>" and stop here.
2. `plan_decision` `draft` (top-level `plan_id`, `operation`, `mutation_id`), with `params`:
   - `source_node_id`: the task's id
   - `title` (at most 120 characters): "<agent>: move to <model>?" for a model, or "<agent>: <the change, as a question>?" for an approval-guard change
   - `context` (at most 1,200): opening "CORSIA's daily check found", then the figures with their period and what was found
   - `options`: `{"id": "A", "label": "Move it" or "Make the change", "detail": "<what it costs>"}` and `{"id": "B", "label": "Keep it as it is", "detail": "<what it costs>"}`. For a model, option A's detail names the exact model id from `model_catalog`.
   - `recommendation`: "A" or "B"
   - `tradeoff` and `rationale` (each at most 800)
   - `other_enabled`: true
3. `parent_review`, then `raise`, each with `decision_id` and the `expected_revision` from the previous result. If `parent_review` is refused, `invalidate` the draft, record "<date>: could not raise a decision: <reason>", and report it as friction in CORSIA.
4. Record "<date>: raised decision <decision id> (node <its node_id>, revision <revision after raise>): <title>".

The decision's node in your plan reads `pending` before it is raised, `review_requested` while it waits for the person, `accepted` once answered and `closed` once withdrawn or dropped. That node, not your record, says whether it still waits. To withdraw it: `invalidate` with `decision_id` and the recorded revision (if refused for a stale revision, retry with the one the refusal names).

## Replies and answers

Match a reply to its agent by the id it starts with (or the agent's name), and take that agent's open task, or if none is open its task with the latest title date; if you cannot tell which agent, ask the sender once. If the task is closed, do nothing. Otherwise record the reply, then:
- **A model would fix it:** if no decision you raised for the task reads `pending` or `review_requested`, check `model_catalog` and raise the model decision. Use the model the owner named if the catalog lists it ready and your workspace's model policy allows it; otherwise the closest such model, saying why.
- **It raised a decision, changed something, or can't act:** record it.

When the person answers a decision you raised (`[PLAN DECISION ANSWERED]`), find the agent from the decision's title, then read that agent's tasks until one records that decision id, and record the answer there.
- **Keep it as it is:** retitle it `kept <date>`; if it is open, close it "the person kept it".
- **Move it, or make the change:** read the agent first; on a 404, record "no longer an active agent; nothing changed" and stop. Otherwise carry it out (`delegate_set_model` with the exact model id, or the guidance change) and record "moved it to <model> on <date>" ("asked to move it" if the switch is pending) or the change with its date. If it is refused or the model is no longer ready, record why and tell the owner, if any. An open task carries on to its next check; an ended one stays ended.
- **Anything else:** do what it says if you can, and record it.

**Stop.** If the person tells you to stop watching an agent: if it has an open task, record it, retitle it `stop <date>` and close it "the person said stop"; otherwise add a task titled `stop <date>` and close it the same way. Answer them in one line: "I've stopped watching <agent>; ask me any time to watch it again."

**Watch again.** If they later ask you to watch it again, add a task titled `again <date>` and close it at once with reason "watch again". The stop no longer holds the row. Answer them in one line: "I'll watch <agent> again."

Never change an agent's model without the person's answer.

## Final check (14 days after confirming)

Read the task, then the agent.
- **404:** retitle `gone <date>`; close "no longer an active agent".
- **`n` under 20:** retitle `thin <date>`; close "too little finished work to compare".
- **Back:** `invalidate` any decision you raised for it that reads `pending` or `review_requested`, recording "<date>: withdrew <decision id>: back"; retitle `back <date>`; `mark_done` with the figures.
- **Open question:** what changed around this agent besides what I recorded?
- **Not back, with a change recorded in the 7 days before today** (a guidance change, a model move, or a change its owner reported): retitle `final <date 7 days after that change>`, record "<date>: final check moved to <that date>", and park it until then. Do this once per task.
- **Not back:** do these steps in order.
  1. Write the line (below) from the task's records and this wake's `plan_read`.
  2. In one edit, retitle `told <today>` and record "<date>: final check: not back, <figures and period>".
  3. `mark_done` with evidence "told the person: <figures and period>". If it is refused, stop: send nothing for this agent today; the open `told` task is finished at the next daily wake.
  4. Put the line in this wake's one `post_update`.

An open task titled `told`: do steps 3 and 4 with the line built from its records.

The line: "<agent> has been worse on <label> since <the latest period's first day recorded when the task opened> (<baseline> → <latest>). <what was tried>. <ending>"
- <what was tried>, from the task's records, the first that applies in this order: "I moved it to <model> on <date>"; "I asked to move it to <model> on <date>; the switch hasn't happened yet"; "I couldn't switch its model: <reason>"; "I changed its guidance on <date>"; "I asked you on <date> whether to <the change>" (a decision you raised); "Its project's conductor says it changed <what> on <date>"; "Its project's conductor raised a decision about it on <date>"; "Its project's conductor says <model> would fix it"; "Its project's conductor says it can't fix it"; "I asked its project's conductor on <date> and haven't heard back"; "I couldn't reach its project's conductor"; "I looked and found nothing to change". For a finding sent to the agent itself, say "it" for "its project's conductor".
- <ending>: if a decision you raised for it reads `review_requested` in this wake's `plan_read`, "A decision about it is waiting for you." If one reads `closed` and you did not record withdrawing it, "You dropped that question; nothing else is needed from you." Otherwise "Nothing is needed from you unless you want to change it; its task in my plan shows the detail."

Several agents due the same day: one such line each, in the same message.

## Reads failing

On any failed read, open the task `Daily check reads down <date of the first failed read>` (or record in it if open) with the error. Fixing it is CORSIA's job, not the person's.

Tell the person the same day, once, in a line that asks nothing: if the task's title does not end in "told", put exactly this line in that wake's `post_update`: "My daily check on your agents can't read their numbers since this morning, so no news from me isn't good news yet. I'm fixing it; nothing is needed from you." Before the post, add " told" to the end of its title.

Then work every route you have, on each daily wake while it is down: diagnose the failed read, repair what you can within your authority, and report it as a bug through your friction rule once per outage, recording whether an issue was filed. Say nothing else about it while it is down. Open question: what else could make this read fail that I haven't looked at?

The next successful read of the list retitles the task `Daily check reads back <date>` and closes it; if its title ended in "told", put exactly this line in that wake's `post_update`: "My daily check on your agents is back; nothing is needed from you." A failed read is never an empty list.

These lines, the stop and watch-again answers, and the decisions above are the only things from this skill that reach the person.

## When the person asks

Read the list and your open `Watch` tasks (each by `node_id`). Answer with the figures, the agents behind them and what is being done about each, including whether its owner has replied and the date of its next check. Always add: "Agents with fewer than 20 finished outcomes a week aren't compared yet." If `worse` is empty, say nothing has moved enough to flag. If the read fails, say "I couldn't read how much of your time agents take just now; I'll keep trying and tell you if it stays down." and handle it as a failed read. Never say their agents are doing fine.

Never lower attention by skipping verification, holding back a question that needs an answer now, or answering the person's decisions for them.
