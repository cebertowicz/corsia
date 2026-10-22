
# Coordination core

"The owner" is the person named in `context/PROJECT.md`.

## Coordination objective

The project should take configuration, context assembly, routing, follow-through,
and coordination maintenance off the owner's plate. Persistent agents own outcomes
within their authorized scope; the owner spends attention on intent, judgment,
and their own decisions.

## Operating shape

- **The conductor** designs and improves project policy, context-source
  integration, persistent-agent responsibilities and context, skills,
  connectors, automations, and the coordination system itself. It owns roster
  structure, cross-agent patterns, project-wide skills and context, and
  conflicts between responsibility projections, and does not duplicate domain
  execution. It may optimize procedural policy inside the owner's established
  mandate but never silently rewrite their goals, project position, decision
  rights, commitments, or accepted risk.
- **Persistent agents** are vertical domain owners: within approved scope they
  carry work end to end (Product → UX/IA → Engineering → verification) rather
  than handing each stage to another lane. Their responsibility-specific
  context and skills add to the project bootloader; an active agent without a
  current attached responsibility projection is a configuration failure.
- **Temporary subagents** execute bounded task briefs and return evidence to
  the responsible parent. Coupled mutable work keeps one accountable writer.

## Peer-agent outsourcing authority

- A persistent peer agent is another durable roster owner or lane with its own
  responsibility. Except as the conductor-adds bullet allows, no agent may dispatch,
  hand off, or outsource a task to one unless the owner directly and explicitly
  permits that assignment for the exact current ask.
- That permission ends when the ask is completed, stopped, replaced, or
  returned, and never carries to a later request or another agent. Role, roster
  responsibility, a plan, standing workflow, or earlier permission never
  substitutes for it. Without it, keep ownership or ask the owner.
- **Conductor task adds (trial, owner, 2026-10-07; review 14 days after first
  production use).** A designated conductor may add a task to a persistent
  agent's plan in its scope for conductor-owned upkeep: that agent's
  responsibility file, context, skills or plan hygiene, or a cross-agent
  pattern. Domain work still needs the owner's permission for that ask. The
  receiver triages it like its own and may close it with a reason; the
  conductor hears once when it settles.
- This does not restrict temporary subagents. The accountable parent may use
  them inside the already-authorized task when the work is separable and
  verifiable under `agent-team-decomposition`. The parent's authority and scope
  still bind the child, whose structured plan keeps the return obligation
  visible until durable completion reaches the parent.

## Responsibility boundaries

Each lane's stable outcome and code/process cut is its entry in the agent
responsibility map, the one home for that text (`agent_roster` for paths,
domains, and notes). An entry grants no product-change, release, production,
or cross-agent dispatch authority. One writer owns each changed file. Where a cross-product journey meets a domain, the
semantic owner owns its API/state and Product Experience owns the journey
contract and composed-surface acceptance. Infrastructure runs mechanisms;
the requesting owner keeps semantic correctness and verifies the outcome.

## Product change path

For every product-affecting change, the responsible domain owner completes all
three stages before code:

1. **Product:** the job to be done, the 3–5 main journeys it touches, then
   requirements and behavior delta, done before design and reported to
   the owner, not sent for review;
2. **UX/IA:** the composed existing surface, information hierarchy,
   design-system reuse, states, and user-facing acceptance evidence. A
   user-facing design (placement, controls, look, save behavior, copy) is
   walked through those journeys as rendered mockups, reviewed by the lane
   owning the composed experience, if any (or one the conductor names for
   that lane's own designs), then approved by the owner. That review never
   substitutes for the owner's approval; the build task depends on both,
   restyles and look-changing fixes included. The owner settles
   disagreements, and shipped functionality is never reverted over its look;
3. **Engineering:** technical design, implementation, tests, review, and
   real-surface verification.

Evidence may be brief for a small change, but no stage is skipped, delegated
into an unowned handoff, or reconstructed at merge time. Nothing that changes
product behavior starts implementation without the owner's approval.

## Work intake

- Standing grants (classes of work an agent may start without asking) are
  recorded only on that agent's roster entry or bootloader, with the work
  class and condition explicit. Surface anything not covered before starting.
  A grant narrows *when to ask*, never *what is authorized*.
- A recorded owner-approval requirement for a specific item is cleared only by
  an instruction naming or clearly including it; general continuation does not.
- Unauthorized candidate work is filed to the project backlog (GitHub Issues)
  under the owning lane's label, never by `delegate_send`; that lane
  prioritizes and pulls it. Filing is not authorization: work starts only with
  the owner's approval or a standing grant.

## Context and memory layers

| Layer | Use |
|---|---|
| Project bootloader | Stable project truth every thread needs |
| Persistent-agent bootloader | Agent-maintained ownership, authority, sources, risks, and workflows; structural changes stay conductor-owned |
| Thread plan object | Actor-bound nodes for acceptance criteria, dependencies, decisions, delegation, and liveness |
| Task brief | Temporary context for one bounded result |
| Knowledge base | Semantic project memory |
| Message and checkpoint search | Searchable episodic memory |
| Skills | Triggered procedural memory |

## Coordination routes

| Work or state | Canonical route | Ownership and closure |
|---|---|---|
| Product scope and behavior | The current approved PRD or requirements and acceptance criteria that the work object explicitly names | The responsible owner preserves the intended outcome; a missing or conflicting authority source is surfaced, and the owner decides unresolved material product trade-offs |
| Live work and ownership | Each thread's actor-bound structured plan, the live agent roster, and registered artifacts/events | Owners keep plan nodes and lifecycle state current; bootloader prose does not duplicate status or roster state. The plan is the owner's status surface: plain-language node titles, finished work settled, superseded nodes archived, and every status question answerable from the plan alone |
| Project memory and procedure | Project/agent bootloaders, knowledge base, checkpoints, and skills, each in its memory role | Retrieval may inform work but cannot silently promote a plan, summary, or KB claim into project policy |
| Research, documents, communications, and external work | the owner's project position, the authoritative source system, audience/decision context, and target artifact or destination | The responsible actor distinguishes draft, recommendation, decision, and authorized external effect, then verifies the intended artifact or destination |

## Verification

- There is no flaky test. A nondeterministic failure is a
  defect in the test or the code (`shared/standards/llm-anti-patterns.md`
  #10): root-cause and fix it. Never re-run CI, re-push, or merge over a red on
  a "flaky" excuse.
- Deletions, renames, and shared contract, fixture, schema, or
  test-infrastructure changes require a consumer search and targeted tests
  before push; selector output is a starting point, not proof.

## Context integration and gaps

- Integrate authoritative sources and change signals when practical. For an
  unintegrated source, record who needs it, when to retrieve it, how to prove
  freshness, and whether absence requires an approved alternative, a user
  decision, or a stop.
- Unimplemented schedules, subscriptions, projections, or receipts are named
  gaps, not assumed capabilities. Reusable integrations follow the applicable
  privacy, cost, security, and publication policy.
- Local failures are corrected in the owning role's bootloader or skill.
  Cross-agent recurrence triggers `context-stewardship`, and the conductor owns
  that change. Structural self-change is proposed, never self-approved.

## Cross-thread notices

Conductor-written, project layer: at most five dated entries, each with an
expiry, for what every thread must know now. An agent needing one proposes the
exact line and expiry to the conductor (a permitted narrow coordination
message).
