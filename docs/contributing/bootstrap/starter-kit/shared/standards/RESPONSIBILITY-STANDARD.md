# RESPONSIBILITY.md standard

**What this is:** the required shape of every persistent agent's bootloader. The bootloader is loaded into the agent's prefix on every turn, so it carries only what a first-turn decision needs and points at everything else. Live status belongs in the structured plan; history belongs in the canonical directory; procedure belongs in skills.

**Budget:** 700 words (about 950 tokens). Over budget means something below has leaked in: status, narration, procedure, or history. Move it, do not trim the required sections.

## Required sections, in this order

1. **Mission.** One sentence: the outcome you own and for whom.
2. **OKRs and purpose.** Two to four objectives with a measurable key result each and its current value or "unmeasured, first measurement due <date>".
3. **Authority.** Two lists: *may decide* and *must bring to the owner or the conductor*. Standing grants (work you may start without asking) go here with their condition. Nothing implied.
4. **Principles and invariants.** The five to ten rules specific to your domain that a change must not break, each tagged `[invariant]` when it is non-negotiable, with a pointer to the standard or contract that states it fully.
5. **Domain map.** The concepts you own, one line each: name, what it is, its canonical state owner, and where its full definition lives. Then the boundaries: which adjacent domain owns each neighboring concept.
6. **Canonical knowledge.** The path of your `shared/<domain>/` directory and what its `INDEX.md` covers: requirements and constraints, concept model and data structures, state matrices, data flows, decision log, failure catalog, quality signals, debt ledger.
7. **Current priorities and initiatives.** Ranked, dated, each with the objective it serves. Superseded items are removed, not struck through.
8. **Quality signals.** The metrics, logs, or checks you watch, the target for each, and where the current value is read.
9. **Recurring workflows.** Cadence, trigger or wake mechanism, what is done, where the result lands, and the date of the last run.
10. **Does not own.** The adjacent responsibilities that look like yours and whose they are.
11. **Lessons.** At most five dated one-liners that changed how you work, each pointing at the fuller record in your decision log.

## Not allowed in the bootloader

- Live task state, progress, "in flight", "waiting on", or done lists: the structured plan owns these.
- Narrative history, retros, incident stories: the canonical directory owns these.
- Step-by-step procedure: a skill owns it; the bootloader names the skill.
- Copies of project-wide policy, coordination rules, or another agent's responsibilities: point at the canonical source.
- Retired authority: a document, port, plan, or lane that no longer exists.

## Keeping it current

Update the bootloader in the same turn as the change that invalidates it (a user correction, a decision, a change in priorities or authority). The weekly self-review in the agent operating contract checks every section against reality and records its date under Recurring workflows. You keep it current; the conductor's audit is the backstop: a bootloader whose priorities or signals are more than two weeks old is stale, and the audit files that as a gap issue under your lane.

## Companion files

A separate `ROLE.md` is not part of this standard. Where one exists, fold its content into the sections above and retire it; two files describing one agent drift.
