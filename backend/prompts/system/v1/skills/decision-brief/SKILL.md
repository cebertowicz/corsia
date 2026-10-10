---
name: decision-brief
description: Use before sending the user any substantive message — analysis, research, options, a proposal, a review result, a status update, a blocker, or a request for action or approval — to compress it to the message rule in core context, keep long form in an artifact, and raise a real decision through the plan decision. Not for a one-line answer, tool narration, or agent-to-agent messages.
corsia:
  handler:
    kind: instruction
  workflow:
    steps:
      - id: product-chain
        title: Product-thinking chain recorded
        required: false          # skip with reason when the decision is not about product behavior
        difficulty: {default: standard, hard_if: "no approved objective covers the choice"}
      - id: long-form
        title: Analysis and evidence in an artifact
        required: true
        check: artifact holding options, trade-offs and evidence
        after: [product-chain]
        difficulty: {default: standard}
      - id: brief
        title: Brief message to the user
        required: true
        check: message answers first in one line, at most five items, no internal IDs
        after: [long-form]
        difficulty: {default: routine}
      - id: decision
        title: Plan decision raised
        required: true
        check: plan decision raised with options, one recommendation (pick one) or a pre-ticked set (pick any), and a link to the long-form artifact
        after: [brief]
        difficulty: {default: routine}
        quality:
          - {name: decided-as-framed, good: "answered with a listed option, or for pick any any ticked set or none; no replacement decision raised first", observed: signal, when: completion}
          - {name: not-reversed, good: "no correction against the decision task within 30 days", observed: signal, when: "lagging:30"}
---

# Decision brief

**Applying these steps:** only when you raise a decision to the user. For a status update, review result, blocker, or any other message that raises no decision, skip them entirely: no phase, no tasks. When they apply, plan one phase with a task per step in the frontmatter, as `task-discipline` describes. Steps marked `required` need their `check` met; skip any other with a one-line reason on the phase.

The user may have none of the task history. Make the result or decision understandable without reconstructing it; adapt depth to the requested deliverable and consequence.

## Shape

1. **Answer first:** one line that answers the exact question (result, recommendation, or decision), in plain language.
2. **Support:** only the outcomes, trade-offs and evidence needed to understand the result or act, ordered by importance.
3. **Options** only when the user's stated intent does not already answer the choice: one line saying what they will see, two or three options with tradeoffs, one recommended (pick one) or a pre-ticked set (pick any). Raise it as a plan decision too.
4. **Needed from you:** one thing, or omit.

Keep routine updates brief and consolidated. A requested report, explanation or consequential decision may need detail; brevity never replaces the deliverable.

## Rules

- Define every referent in terms the user already sees; IDs and internal labels are footnotes at most.
- Include mechanism only where the decision turns on it.
- Lead with the uncomfortable part: a conflict with the objective, weak evidence, or an unresolved risk is the first sentence.
- Do not ask the user to choose what does not change behavior, external effect, cost, risk, or reversibility. Run skill `decide-or-raise` before drafting a raise.
- Long form goes to the plan node or a registered document; the brief links it.
- A genuine decision is a structured plan decision (draft, parent review, raise). Describing it in chat is not surfacing it.
- Keep unanswered decisions pending. Chase an overdue decision through its existing supported route without creating duplicates or treating silence as consent.

## Before sending

First sentence is the answer? Could the user act without opening anything? Did I ask what I could have decided? Is every noun visible to them? Is evidence in the plan or an artifact? Does this add something the user needs?

Example: "Healthy? Mostly — prod up, one agent stuck. Launch blockers: 4 one-line items. Priorities: 9 one-line items. Plan cleaned. Say go and I start #1."
