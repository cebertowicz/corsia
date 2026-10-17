---
name: memory-placement
description: Use when the user corrects your work or states a rule, preference, or fact that should outlive this turn, when you infer a reusable rule of your own to try, and when a checkpoint review keeps or retires a continuity entry; pick the one existing destination whose audience will have it in time. Not for live task state (use the plan) or for editing files another owner writes.
---

# Memory placement

Put a lesson where its next reader already looks. Prefer the existing canonical source; if the destination is missing, propose it to its owner instead of inventing a parallel memory system.

## 1. Extract the lesson

Apply the core causal-lesson check. Preserve incident evidence in the reference, but place the transferable rule separately. If the cause is unclear, record the uncertainty and retain the entry.

## 2. Answer two questions

**Who must have it?** Thread (this work only), Agent (this owner's future work; include relevant rules in child briefs), Project (everyone in the project), Everywhere (all projects; how the user works generally).

**When must they have it?** Every turn (a rule or preference changing first decisions), when a task starts (a procedure for a recognizable task), when looked up (a fact or reference).

| Who ↓ · When → | Every turn | Task start | Looked up |
|---|---|---|---|
| Thread | continuity; place nothing | continuity | the thread's `working/` files |
| Agent | `.threads/<id>/context/` (`RESPONSIBILITY.md` for a persistent agent) | `.threads/<id>/skills/<name>/SKILL.md` | the agent's `shared/<domain>/` reference |
| Project | project `context/` | project `skills/` | `shared/<category>/` |
| Everywhere | workspace user profile | workspace `skills/` | workspace user reference |

`<id>` is your thread id; paths are under your project unless marked workspace.

## 3. Rules

1. **Who writes.** You write the Thread and Agent rows for your own thread. Project and Everywhere rows belong to the conductor: record `continuity_edit` `place` with verdict `proposed` and the `placement`, and CORSIA files it as a proposal. Never write a file you do not own or place the lesson elsewhere as a workaround.
2. **Same turn.** Place it when the correction arrives. A checkpoint review may later sweep recorded continuity, but it cannot recover a correction you did not record. Each checkpoint offers a batch of your entries, oldest-reviewed first, sized to the room its fork has, and every entry comes round in turn. Give each offered entry one verdict: `select` (write it, per rules 1 and 4), `already` (rule 3), `drop` with a reason, or `reaffirm` with a reason why it still belongs in continuity (a freeze with an end date, say). A lesson is never reaffirmed, and `select` answered q1 `thread` is refused for one. A reaffirmed entry's reason is shown when it is offered again.
3. **Duplicates first.** Read the destination and earlier decisions on the subject. If it already says it, write nothing and record `place` `already` with that file and line (rule 7); a rule that exists but was not followed is a delivery or execution problem (skill `context-stewardship`), not a missing rule.
4. **Widen deliberately.** In Thread and Agent rows the user's words may stay verbatim. In Project and Everywhere rows write an impersonal rule in that file's voice: no quote, name, private detail, or incident.
5. **User provenance.** The user profile and user reference take only what the user said in a message a human wrote, not a subagent's return or another agent's claim.
6. **Scope words decide reach.** "For this report", "here", "this time" mean Thread. "Always", "every project", "everyone" widen it.
7. **Retire by receipt.** After writing, record `place` with verdict `written`, the `destination` file and a `quote` of the exact line that carries the lesson. The entry retires only if this thread receives that file and it contains the line; a refusal says what to correct. A `proposed` entry is kept until CORSIA files it.
8. **Your own hypothesis is a trial.** An unproven rule you inferred about how to work gets no permanent line. `place` its entry with verdict `trial`: `placement` q1 `agent`, a new `skill_name`, a `skill_description` (when it applies), and `trial` with `kind` `hypothesis`, `review_after` and `boundary` (when it does not). CORSIA writes the skill and opens your review task. It joins your skills list, marked `(trial, unproven)`, at its next rebuild, not in this conversation, and leaves at the first rebuild after its review date; a conversation already listing it keeps it. It grants no permission.
9. **A person's words are never a trial.** Their instruction, preference or constraint goes to its permanent home. If your hypothesis rests on their words, keep those words in their own entry or place them first, citing the same message, then add `derivation` (how your rule follows) and `literal_ref` (where they are kept).
10. **Report it** in one plain line: what changed and where.

## 4. Triage a correction

When a person corrects you, or a correction note arrives in your turn: fix the work first. The triage is working state, not part of your reply.

**Tier.** Set one. You may raise a proposed tier, never lower a Serious one.
- **Not a correction** (a question, an approval, a changed mind, a new request): do it; nothing else.
- **Instruction** (a rule the person states): place their words in its permanent home (section 2). Never a trial.
- **Note** (a one-off, low-stakes detail): fix it; if a delivered rule or fact was wrong, apply cause 1 too.
- **Miss** (the work did not do what was asked, and they had to catch it): fix it, then find the cause.
- **Serious** (the same miss again; a consequence such as lost data, something public, money or a deadline; a clearly furious person): find the cause now and search your watch items and records for the same pattern. Anger deepens the review; it never by itself justifies a new rule.

**Cause.** Ask in order and stop at the first yes. Write the remedy for the activity, not the one output.
1. **Wrong rule:** a delivered rule, lesson or fact is false or stale. Fix it at its source now if you own it; otherwise send it to its owner (Whose is it). Never edit a generated System copy. No trial, no watch.
2. **No foundation:** a high-stakes activity has no ground rules or no process. Set up both with `task-discipline`'s "No established workflow".
3. **Rule gap:** the rules miss this case or cover it too narrowly. Amend them at the activity's level: a trial if it is your inference, the permanent home if the person said it.
4. **Process gap:** no step applies or checks the rule at the right moment. Add the step to the workflow or gate.
5. **Not delivered:** the rule exists but did not reach you then. Fix its trigger, placement or scope (skill `context-stewardship`); no second copy.
6. **Not followed:** delivered and clear, but not followed. No new text: watch it, and on a repeat recommend a control to whoever owns the gate or tool.
7. **Can't be done:** the system lacks the mechanism. File it for the owning lane with the correction as evidence.

**Whose is it.** Name who owns the rule, step or gate that failed. If another owner does, fix only your output and your own part, and add no rule or check of yours to cover their gap. Send that owner one message with the correction, your cause answer and the evidence, never a task on their plan; a paused owner's message is held, not rerouted.

**Watch or act.** A Miss with an unclear cause, or Not followed: record it as a continuity entry, then `place` that entry with verdict `trial`, rule 8's fields, and `trial.state` `watch`; its text is one general line naming the decision that went wrong. A watch is never delivered; its review asks whether it recurred. A repeat on a different task, or a Serious miss with a clear rule gap: `trial.state` `trial`. Either one placed in the turn the note arrived links to the correction automatically; one placed in a later turn does not, so place it now.

## Wrong and right

The user tells a persistent agent: "Stop posting the daily summary in chat. Put it in the plan."
- **Wrong:** add the quote to project rules yourself.
- **Right:** Agent scope, every turn: add the rule to your own `RESPONSIBILITY.md`, then `place` it `written` with that file and the line. If the user says everyone should, `place` it `proposed` with the text "Keep daily summaries in the plan, not in chat."

After three declined invitations you suspect people in Europe accept morning Pacific times.
- **Wrong:** add it to `RESPONSIBILITY.md` or hand-write a `SKILL.md`.
- **Right:** `place` it `trial`: for proposing a time to someone in Europe, not when they stated availability. At its review date, retire, promote, extend or revise it with evidence.

Done when `place` accepted the receipt (or queued the proposal or trial) and the reply says where it went.
