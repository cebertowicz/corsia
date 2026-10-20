---
name: apply-feedback
description: Use whenever feedback arrives on anything you made or own (a comment, correction, reviewer finding, failed check or requested change), on any kind of work, before you edit — find what it means beyond the spot it names, change everything it touches, and re-read the whole as its audience. Not for a first draft no one has commented on.
---

# Apply feedback to the whole

A comment points at a symptom in one spot. The change it asks for nearly always reaches other places: a definition, an example, a summary, a count, a test, another document, an agent that relies on it. Patch only the spot and the person finds every gap themselves. They should never be the one who checks that the whole still fits together.

## 1. Read everything before editing

- Read every open comment before changing any of them, and the resolved ones on the same parts. The person's own earlier words on the subject count too: search their messages, not only agents' summaries of them.
- Never start editing after the first comment.

## 2. Find the cause, then group

For each comment, ask what made the thing wrong and where else that is also true. Group comments that share a cause and fix each cause once.

- Wrong: "Agents shouldn't ask here" → edit that one step.
- Right: also the option that assumed an ask, the example that showed one, the summary line, the defined term, and the linked document that promises the same behavior.

A question ("when does this run?", "who owns it?") means the result failed to answer it. Answer it in the result for every moving part, not only the one it was left on, and not only in a reply.

## 3. Map everything the change touches

Search by concept, not only by the comment's words: names, synonyms, IDs, numbers, file and section references.

- **Inside the result:** every section, table, example, definition, count, status line, recommendation and open question that states or depends on the changed thing.
- **Outside it:** documents that cite it (indexes, roadmaps, decision logs), code and its callers, tests and fixtures, plan tasks and their criteria, skills, messages already sent, and other owners' work that relies on it.

List what you found. Each item gets updated, or left alone with a stated reason.

## 4. Change the whole, and keep it simple

- Update every place on the list. Delete what stopped being true; don't leave it with a caveat.
- After three or more rounds of patches to one part, or reviewer replacements that each bring their own wording, rewrite that part once from the current intent instead of splicing in another patch.
- Feedback must not grow the thing. Add a term, mechanism, section or option only if it replaces one. When reviewers disagree, settle it against the person's intent; don't merge both versions.
- When you change something another owner holds, tell them in one message naming exactly what moved.

## 5. Re-read the whole as its audience

Read it top to bottom, cold, as the person or consumer who will use it, not as the author of the change. Check:

- **The original ask:** it still answers it.
- **One model:** each concept has one name, every term is defined before use, nothing contradicts anything else.
- **Nothing dangling:** no stale reference, count, link or leftover of the old version.
- **Every moving part says** what it is, who owns it, what starts it or when it runs, and what the person sees.
- **The next comment:** what will they ask next? Answer it now.
- **The strongest counterexample:** the case where your change breaks.

Then run the check that fits the work:

| Work | Check |
|---|---|
| Code | Affected tests, a search for every caller and consumer, and the whole final diff |
| Interface | The whole composed screen, rendered |
| Documents and data | Recompute numbers, open links, view the rendered output |
| Plans | Criteria and dependencies still match the outcome |
| Messages | Read it as the recipient, with nothing else open |

## 6. Close the loop

- Reply to each comment with what changed and where else it changed, or why you left something.
- Update the plan task. If the person has an open ask on this result, make sure it shows the new version.
- The same kind of feedback twice is a lesson (skill `memory-placement`).
- Hand back only once this re-read passes. If it can't yet, say what is still open; don't let the person find it.

## Failure signal

This skill failed if the person or a reviewer finds, after your change, a place you missed: a contradiction, a stale line, an unanswered when, who or what, or a new concept that only made things more complicated.
