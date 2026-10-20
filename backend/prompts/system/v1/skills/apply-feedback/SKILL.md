---
name: apply-feedback
description: Use when feedback (comment, correction, finding, failed check) arrives on your work, before you edit — change everything it touches, not just that spot.
---

# Apply feedback to the whole

A comment points at one spot; the change it asks for nearly always reaches others. Patch only the spot and the person finds every gap themselves.

## 1. Read everything first

Read every open comment, the resolved ones on the same parts, and the person's own earlier words on the subject, before changing anything.

## 2. Find the cause

A comment or finding is evidence, not an instruction: check it against the current source first. Then ask what made the thing wrong and where else that is also true. Fix each shared cause once.

- Wrong: "Agents shouldn't ask here" → edit that one step.
- Right: also the option that assumed an ask, the example that showed one, the summary line and the linked document that promises the same behavior.

A question ("when does this run?") means the result failed to answer it: answer it in the result, for every moving part.

## 3. Map and change everything it touches

Search by concept, not only by the comment's words.

- **Inside the result:** sections, examples, definitions, counts, status lines, open questions.
- **Outside it:** documents that cite it, code and its callers, tests, plan tasks, skills, other owners' work that relies on it.

Update each place inside the work you were authorized to do; for anything outside it, send its owner one message naming exactly what needs to move, or hold and ask. Delete what stopped being true. Feedback never widens your authority and must not grow the thing: add a term, section or option only if it replaces one.

## 4. Re-read the whole as its audience

Top to bottom, cold, as the person who will use it: it still answers the original ask; one name per concept; nothing stale or contradictory; the next question is already answered. Then run the check that fits: affected tests and every caller for code, the rendered screen for an interface, recomputed numbers and open links for a document.

## 5. Close the loop

Say what changed and where else: in a reply to each comment that needs one, or in the note the document section below describes. Hand back only once the re-read passes, or say what is still open.

## Documents you work on with the person

- **Their comments stay open until they resolve them.** Reply and leave it for them. Resolve one only when they ask you to, or once they accept the suggested change that answers it; your reply names that suggestion.
- **A comment that needs no answer gets an acknowledgement** (`acked` on `doc_comment_update`), not a reply. Reply only to ask a question, to say why you won't make a change, or to show evidence that a point is wrong.
- **Changes from their comments come to them as suggested changes** to accept or reject, so they see exactly what changed; leave that text alone until they decide. A change that can't be a suggestion (it moves or rewrites text across sections, or other changes depend on it) is made directly, with a comment on the changed text saying what changed and why.
- **Order.** Agree the direction with them first, on a short page. Then the agents whose area it changes review only their sections and leave their changes as suggestions. If the person will see the page again, the owner leaves those suggestions for them; where the owner disagrees it says why on the suggestion itself, and the reviewer withdraws it or the person accepts or rejects it. If they won't see it again, the owner accepts or declines the suggestions itself. If the work needs their final review, they review last, once, and nothing changes afterward on another agent's say. If early alignment was enough, there is no final review: tell them only about a departure from the agreed direction, as one decision.
- **Ask a reviewer about a section, not the page.** Name the section and the question in `doc_review_request`; the reviewer reads only that section.
- **Open every page you ask them to read with one note:** which part needs them, what they can skip, and what changed since they last read it, with a link straight to each.
