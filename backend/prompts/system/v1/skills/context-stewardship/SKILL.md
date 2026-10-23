---
name: context-stewardship
description: Use when evidence ties a wrong decision to delivered context, cross-agent behavior may need promotion to a shared layer, a System instruction seems wrong or missing, or when auditing context delivery, duplication or token cost. Not for local edits with no delivery, structural or cross-agent effect.
---

# Context stewardship

Improve agent behavior, not prose volume. This covers context-delivery cost and placement. A design's total operating cost belongs to skill `product-thinking`.

## Diagnose first

1. Name the observed failure or desired behavior and who it affects.
2. Find the shared cause, not the incident's detail. Decide which it is:
   - **Missing rule:** no delivered source says it.
   - **Delivery:** the rule exists but did not reach this role, harness, or trigger.
   - **Execution:** it was delivered and clear, and was not followed.
   - **Capability:** the system lacks a mechanism, and prose would only hide that.
3. Inspect what was actually delivered for representative roles and triggers: the effective-context view, if one is available to you, else the source files by layer. File length is a proxy for tokens, not proof of delivery; say which you used.

Delivery order: System, workspace, project, role, thread, then skills. Each layer holds one kind of thing:

| Layer | Holds |
|---|---|
| System / workspace | Invariants needed on nearly every turn, for every project |
| Project bootloader | Stable project truth every actor needs |
| Responsibility bootloader | One agent's ownership, authority, risks, sources |
| Skill | Conditional procedure for a recognizable task or failure state |
| Plan | Live objective, acceptance, dependencies, decisions |
| Shared reference | Evidence and reference, retrieved not injected |

System content is projected into `.corsia/context` and `.corsia/skills`; runtime edits belong only in the editable layers you own. Upstream changes follow the source owner's review route.

## Make each source earn its cost

Does it change a decision or prevent an observed failure? Is it current, authoritative, and uniquely owned here? Does every recipient need it every turn? Does another source repeat, conflict with, or hold the real detail? Would a shorter rule plus one reference keep the behavior? Search all delivered layers and references for duplicates and conflicts before adding anything, and update the existing canonical source rather than adding a second rule.

Keep authority, safety, and invariants even when costly. Cut duplication, stale state, history, and conditional procedure from always-loaded layers, not the invariant. An over-large source is split into a short rule plus a retrieved reference, and the split is checked by an independent reader.

## Promote

A local lesson is promoted when the same failure recurs across agents or its cause is shared. Propose the exact line, destination layer, and evidence to the conductor, who owns project-wide sources. Decline duplicates. For a wrong or missing System instruction, preserve the failing scenario, exact proposed text and expected behavior; ask the conductor to check placement and conflicts. Use a configured upstream review route if one exists. If its owner or route is unavailable, record that capability gap and retain the proposal; do not claim submission, edit a generated projection or hide a replacement in a shadowed local copy. A project conductor also looks for these patterns itself, on the cadence its role context gives; the procedure is in `references/cross-agent-read.md`.

## Learning reports

A `measurement.learning_report` counts one scope's day: corrections by source; lessons recorded (in use or held), confirmed, consolidated or repeated; experiments started or decided; wins. Each count carries record references, and `null` means not recorded, never zero. A project conductor runs its cross-agent read on it; the CORSIA agent's workspace report holds only cross-project items. Route what you find at once.

Only the daily report (`report_key` ending `:daily`) writes to the person: one consolidated `post_update` for the scope, or its lines added to that turn's existing update, including what that day's early reads found. An early read never posts. On a day the person corrected agents, it opens with one floor line: how many times they corrected agents, as one number (chat, document comments and decision answers added together; a reviewer's send-back is never one), and the report's lessons that day: how many recorded, in use by agents, and not in use yet. It never asks the person to act. Also add a line for: a new theme across two or more agents, routed; a trial kept, or a lesson consolidated; an experiment started or decided; a win (a trial kept for quality or cost, with its measure); an agent repeating a miss after its own lesson. With no correction and none of these, send nothing. At most five lines in plain words, never a mechanism, id or path; each added line names the agents and the fix. The CORSIA agent sends at most one cross-project line a day. No report is a quiet day: post nothing, and do not look for one.

## Prove it

Before editing, write a small scenario set: the failing task shape, an ordinary success, a boundary case that must not over-trigger, the expected action and forbidden action, and the role and delivered sources. Compare behavior, correction rounds, completion evidence, and cost before and after the edit, on fresh turns with the new delivered sources. Size, stage and grade that comparison with skill `experiment-design`; the scenario set is its first stage. Where equivalent inputs take different paths, the expected fix is naming the competing concepts and their canonical owner, not patching one branch.

Record before and after size by layer, scenarios and outcomes, unresolved measurement gaps, and the next measurement trigger. Revert changes that cut size but worsen behavior. Trigger this after a context-attributable correction, a repeated cross-agent failure, a proposed promotion, or a delivery audit, not after every local edit.
