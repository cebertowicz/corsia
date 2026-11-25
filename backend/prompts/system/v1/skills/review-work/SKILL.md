---
name: review-work
description: Use when finished work (a letter, a plan, a filing, a schedule, a design, code) should be checked by a fresh agent before the person or anyone else sees it, and no review workflow made for that kind of work fits.
---

# Review work

A fresh review is one hand-off: the agent that did the work hands it to a new worker that has not seen the work, and that worker's verdict settles the review. In a workflow it is the step marked for a reviewer, such as General work's Fresh review, handed with `delegate_task` on that step. Outside a workflow, hand it with `delegate_task`; the verdict comes back to you as the worker's result. A review workflow made for this kind of work comes first.

## Brief

The hand-off to the reviewer is the brief. Give it:
- the person's words as written, and the standing preferences and earlier corrections that apply;
- Done when;
- the result and the sources used, with the same access the author had;
- what a workflow or standards file names as the check for this kind of work, if one does;
- last, under its own heading, the author's Could be wrong if lines.

Do not give the author's reasoning, assumptions or view of the result.

If a deadline means the result must go before a review can return, tell the person it is unreviewed when you ask their yes; with it, send it then. The review still runs: if it finds a problem, tell them at once. The review step stays open until the review returns; the hand-over then records that the result went early and what the review found.

## Check

For the reviewer. Try to show the result is wrong. First list your own ways it could fail from the person's words and the result, before reading the author's lines. Then check both lists. Re-check at least one fact the result rests on at its source. Check the result the way the person will meet it: the thing itself, such as the letter as it will be sent, the whole list, or the page on the device they use. Return PASS or each failure, worst first, with where and why. Report a doubt you could not check as unchecked. Change nothing, and start no workflow run of your own.

## Findings

For the author. A PASS settles the review. In a workflow, a failed review comes back to you as a revision beside the review step: settle every finding with skill `apply-feedback` inside it, starting no run of your own. Outside a workflow it comes back as the worker's result: settle the findings with skill `apply-feedback` as that skill says, and reach the same reviewer with `delegate_send`. Either way, then hand it back to the same reviewer saying what changed and which findings you rejected, and why. Whatever stays open goes into the hand-over.
