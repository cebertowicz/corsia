---
name: decision-brief
description: Use before sending the user any substantive message (analysis, options, a proposal, a review result, a status update, a blocker or a request) to compress it to the message rule in skill `asking-the-person`, keep long form in an artifact, and raise a choice the way skill `asking-the-person` picks. Not for a one-line answer, tool narration, or agent-to-agent messages.
metadata:
  corsia-data: corsia.yaml
  corsia-format: "1"
---

# Decision brief

**Applying these steps:** only when you raise a decision to the user. For a status update, review result, blocker, or any other message that raises no decision, skip them entirely: no phase, no tasks. When they apply, add one phase (`kind: phase`) with `workflow_ref: decision-brief`, as `task-discipline` describes; CORSIA builds a task per step. Steps marked `required` need their `check` met; skip any other when you reach it, with `close_node` and a one-line reason.

## Shape

The user arrives cold, with none of the task history. Everything they need to act sits at the top; what follows is optional detail, and nothing that bears on the choice is cut. Adapt depth to the deliverable and consequence.

1. **Answer first:** one line that answers the exact question (result, or what is being decided and your recommendation), in plain language.
2. **Goal and terms:** the goal in your own words, so they can spot a misalignment, and every new term defined.
3. **Why it matters**, and, only when their stated intent does not already answer the choice, two or three options with what each preserves and costs.
4. **Up front, not buried:** the weakest assumptions it rests on, what they are most likely to disagree with, and the calls you made for them.
5. **Show it** where a picture is clearer than prose, embedded as relative images: a diagram for a flow or a sequence, a sketch or mockup for something they will see, a table to compare options. Choose the kind from the work; use none where prose is clearer. A departure from what they agreed is shown this way, never only described.
6. **Needed from you:** one thing, or omit.

Keep routine updates brief and consolidated. A requested report, explanation or consequential decision may need detail; brevity never replaces the deliverable.

## The card

A plan decision is read in one pass by someone switching in from other work.

- The question first, in one plain sentence with one reading; conditions come after it, never piled up before it.
- The person's own words for things, as the project uses them; no invented or internal terms. A term the card cannot do without is defined on the card.
- Only what deciding needs: what is being decided, what each option changes for them, your recommendation and why. Leave out everything else.
- Each option answers the question directly, and the tradeoff says what each gains and gives up in the question's own terms.
- If they would need to open a file to decide, or must see a change to something laid out for them (a layout or a design) to judge it, it is not a card: send the brief instead (skill `asking-the-person`).

## Rules

- Define every referent in terms the user already sees; IDs and internal labels are footnotes at most.
- Include mechanism only where the decision turns on it.
- Lead with the uncomfortable part: a conflict with the objective, weak evidence, or an unresolved risk is the first sentence.
- Do not ask the user to choose what does not change behavior, external effect, cost, risk, or reversibility. Run skill `decide-or-raise` before drafting a raise.
- Long form goes to the plan node or a registered document, linked as optional detail. A document they need in order to decide is the ask itself (below), never a card's link.
- A genuine decision is raised: a plan decision when the card alone carries it cold (options, one recommended or a pre-ticked set), otherwise an artifact ask. Describing it in chat is not surfacing it.
- An artifact ask, for a brief or something they must judge itself (a draft to send, a design): register the document as yours (`artifact_register`), then `plan_update` `block_node` with `owner_kind: user` and `ask_artifact_id`. To have the person judge something, use this ask, not `request_review`.
- Keep unanswered decisions pending. Chase an overdue decision through its existing supported route without creating duplicates or treating silence as consent.

## Before sending

First sentence is the answer? Could someone with none of the history act from the top alone? Did I ask what I could have decided? Is every noun visible to them? Is evidence in the plan or an artifact? Does this add something the user needs?

Example: "Healthy? Mostly — prod up, one agent stuck. Launch blockers: 4 one-line items. Priorities: 9 one-line items. Plan cleaned. Say go and I start #1."
