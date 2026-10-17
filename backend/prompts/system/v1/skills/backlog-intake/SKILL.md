---
name: backlog-intake
description: Use when filing, triaging, labeling, or deduplicating issues or backlog items in the project's tracker, or when a report should become one — classify the type, follow the tracker's templates with reproduction or journey evidence, check duplicates and documented intent, route to the owning label, state severity and success signal. Not for ranking items or fixing them.
---

# Backlog intake

An issue is a contract for future attention. A vague one costs the owner a round trip each time it is touched; a good one can be picked up cold.

## Ground

1. Read the tracker's templates (for GitHub, `.github/ISSUE_TEMPLATE/`). Their fields are the minimum. If the tracker has none, use the table below.
2. Search open and closed items by the reported words and underlying concept. Link or reopen rather than create.
3. Verify the current relevant artifact, source or deployed surface. If source already contains a fix but the affected deployment does not, record that distinction.
4. For anything called a defect, establish intent as skill `engineering-standards` describes for code, or the equivalent for documents or processes: documentation, tests or checks asserting it, and the introducing change or requirement. Deliberate behavior may be a design disagreement; compare it with the current approved requirement before classifying it.
5. Find the owner and label from the live roster or the project's routing. Filing under a label routes; it does not authorize work.

## Classify

| Type | Use when | Must include |
|---|---|---|
| Bug | Behavior contradicts approved or clearly expected behavior and can be reproduced | Expected vs observed, reproduction, surface, severity, environment and build |
| Enhancement | Small refinement to something that exists | User job now vs after, objective served, success signal |
| Feature | New or multi-slice capability | Summary of the product chain (skill `product-thinking`) |
| Discussion | Open question without a decided outcome | The question, options so far, who must decide |
| Measurement proposal | A behavior to measure, not ship | Behavior, scenario, pass bar, owner |

Group symptoms with a shared cause into one owned issue; separate unrelated causes and cross-link dependencies.

## Write it

Before any external create, edit, or comment, inspect the complete outgoing title, body, attachments, and links. Never quote, name, or attribute a private user or private feedback without explicit permission for that attribution and audience; "users report" still reveals a source. State verified problems and requirements impersonally. Permission to publish is not permission to attribute.

- Title: `[type] <one line a stranger understands>`.
- Body follows the template order. Reproduction is steps, a scenario, or a trace; "sometimes broken" is not one.
- Environment: which deployment or workspace, the exact build, model and harness when relevant.
- Evidence: screenshot, excerpt, or measurement; for UI the composed real surface.
- Severity per the scale, with a one-clause reason.
- A suspected cause is labeled a hypothesis and names where it likely lives; it is not the fix.
- Success signal: the scenario that passes or number that moves.

## Hygiene

Close duplicates with a link and one sentence. An issue changing user-visible behavior notes that owner approval precedes implementation. Filing is intake, not delegation: do not file to hand off work you own. Tell the user in one plain sentence with the URL: what is wrong, who owns it, how severe. The skill failed if the picking owner has to ask what it means, cannot reproduce it, finds it fixed, or finds a design disagreement labeled a bug.
