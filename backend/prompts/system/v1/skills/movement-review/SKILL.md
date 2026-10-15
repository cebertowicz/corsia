---
name: movement-review
description: Use when a `measurement.movement_digest` note or wake arrives, listing series that moved outside their normal range with the changes made before each. For the CORSIA agent (all series) and a project conductor (its own project's series only). Not for pulling dashboards on your own, or for reviewing a single agent's work.
corsia:
  handler:
    kind: instruction
---

# Movement review

The digest is built without a model and arrives as a note, or as a wake when a person-attention or cost series crossed its threshold. Each move names the series, its owner agent, the value, its own normal range, and `preceding_changes`: instruction, skill, workflow, model and roster changes that happened before it. A preceding change is context, never proof of cause.

A project conductor works only the digest of its own project, against that project's objectives. Do not read other scopes' series.

For each move, in order:

1. **Ask how to improve it.** State what the series measures and which way better is. A move toward better needs no action; note it for step 5.
2. **Check preceding changes.** Read each listed change. Say "happened before", and whether it plausibly bears on this series and why. With none listed, say so and do not guess one. Before proposing a fix, dig several whys deep from evidence: what the owner did differently, where it went wrong, and why; a first plausible reason is a hypothesis, not a finding. Ask: what would have to be true for this move to happen with no listed change at all?
3. **Check for missing context.** Does the owner lack something it needs: a source it never reads or should read and has not, a stale document, an unclear objective? Verify against the owner's own bootloader and sources before you claim it. Propose the addition; do not install it. Ask: what would the owner need that no listed change or source explains?
4. **Send the finding once** to the owner agent, or to its project's conductor where it has one, with `delegate_send`: series, value against range, the preceding change if any, the missing context if any, and one proposed fix. One message per owner per digest, covering all of that owner's moves. Do not resend an unanswered finding.
5. **Close the loop.** Record the series and date on your plan. On the next digest, check whether it moved back into range. Still out: ask the owner what it found, and raise it to the person only if the fix needs their word.

The person's word is still needed for a change to product behaviour, spending, or a new source's access. A digest is not a reason to message the person: no `post_update` for a move you can route to its owner.
