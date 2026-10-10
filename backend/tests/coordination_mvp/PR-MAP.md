# Coordination MVP — S0–S7 Slice / Checkpoint Map

`owner first-implementation-gate thread · status implementation-planning · branch feature/v2-coordination-mvp (off origin/dev) · inputs COORDINATION-MVP-IMPLEMENTATION-SEQUENCING-RFC, RFC-GENERIC-EVENT-ROUTING-BINDING, EVENT-FLOW-LAUNCH-SURFACE-CUT, EVENT-SUBSTRATE-ACCEPTANCE, PRD-{EVENT-MODEL,EVENT-DELIVERY,AUTOMATION,PARENT-REVIEW-GATE}`

This is the implementation-gate map the sequencing RFC requires before code begins. It maps the approved S0–S7 slices to **checkpoints on a dedicated integration branch**, gives dependency order, tests/gates, and hard stop conditions. Short by design.

**Branch model:** S0–S7 are **checkpoint commits/slices on `feature/v2-coordination-mvp`** (based off `origin/dev`), NOT a stream of standalone dev PRs. A WIP/refactor slice need not be individually coherent/green on `dev` — only the branch's **final integration PR** must be coherent + fully green when the **conductor explicitly promotes** it to `dev`. Exception: the conductor may later decide a specific slice is independently coherent and safe and pull it forward as its own PR. Replace-don't-preserve and all gates below still apply at each checkpoint.

## Implementation gate — confirmed

The 4 gate conditions (sequencing RFC §Implementation Gate) all hold:

1. **No slice requires changing the approved product model.** Every slice rides the locked ownership split and the launch cut. ✅
2. **No launch-minimum user-facing path is knowingly broken.** The launch cut (`EVENT-FLOW-LAUNCH-SURFACE-CUT`) exposes only destinations/conditions/controls that exist at launch; the rest is honest fast-follow. ✅
3. **No connector-specific router is introduced.** Proven by `RFC-GENERIC-EVENT-ROUTING-BINDING` (one `resolve_targets`, connector differences = declared data) and *enforced* by the S0 `test_guard_no_connector_specific_routing` ratchet. ✅
4. **No open high-risk/low-confidence finding blocks code.** The single open item is the **peer-scope rule-conflict default** (R3/S1): recommended default = *one-scope-wins by precedence; equal-precedence tie → loud binder-repair artifact; multi-scope fan-out opt-in + audited*. It is already on the owner's surface, is engineering-decided, and is **not a model break** — so no stop. ✅

**Anchor on existing code:** the GitHub artifact-ownership path already lives in `backend/events/{identity,router,delivery,dead_letter,guard}.py` (PATH A). The MVP **generalizes** this path; it does not fork it. Replace-don't-preserve: the legacy GitHub-shaped `route_event` is migrated onto the generic `resolve_targets`, not kept beside it.

## Dependency graph

```
S0 (fixtures + gates)  ── foundational, blocks nothing's design, gates everything's tests
   │
S1 (Event Model: envelope, IdentityFormula, RelationshipDecl, event_class, ownerless_ok, dedupe)
   │
S2 (Artifacts: register_or_resolve → ArtifactRecord{owner, subscribers[kind]}, archive≠sub-archive)
   ├──────────────┐
S3 (Connections   S5 (Parent review + Artifact Review Profiles)   ← needs S2; route-back needs S4
   rule binder)   │
   └──────┬───────┘
S4 (Event Delivery: resolve_targets + disposition table + live health sink)   ← keystone; needs S1,S2,S3
   ├──────────────┐
S6 (User attention  S7 (Reliability automations: watches, liveness, restart)
   surfaces UI/API) │
```

Critical path: **S0 → S1 → S2 → S3 → S4**, then S5/S6/S7 fan out (S5 can start after S2 and only needs S4 for route-back delivery; S6 needs S4 closeout-emit + S5 disposition; S7 needs S4 watch-expiry routing).

## Slice / checkpoint map

Each row is a **checkpoint on `feature/v2-coordination-mvp`** (commit, not a dev PR). The `Depends` / `Tests` / `Hard stop` columns are unchanged by the branch model. Where a slice is large it is split into sub-checkpoints (a/b). Final **one coherent integration PR → `dev`** lands the whole branch on conductor promotion.

| Checkpoint | Slice | Scope | Depends | Tests / gate | Hard stop |
|----|-------|-------|---------|--------------|-----------|
| **S0** | S0 | Contract fixtures, AC→test/gate map, 3 guard ratchets, this map. No behavior change. | — | `tests/coordination_mvp/` green; `git diff --check`. | Fixture can't express a launch scenario as declared data. |
| **S1** | S1 | `EventEnvelope` (event_id=IdentityFormula(facts), facts, relationships, artifact_ref, provenance, event_class, ownerless_ok); `RelationshipDecl`/`IdentityFormula` records; one normalized `event_id` across intake/delivery/automation; state-version in internal-event identity. Migrate GitHub identity onto declared formula. | S0 | dedupe/identity scenarios (Gmail first-touch stable id, GitHub redeliver→same id, doc-comment version facts, complete→reopen→complete distinct). AC-1.*, AC-5.9. | A launch event type has **no stable identity formula** → return to PRD Factory. |
| **S2** | S2 | `Artifacts.register_or_resolve(identity, owner_intent?)` idempotent → `ArtifactRecord{owner, subscribers:[{thread,kind∈owner|subscriber|parent_chain}], lifecycle}`; archive vs subscription-archive distinct. | S1 | first-register vs resolve same shape; parent_chain can't become owner; closeout≠archive. AC-2.1, AC-2.2, AC-4.12. | First registration and later resolve need **different record shapes**. |
| **S3** | S3 | Connections rule binder: ordered rules over Event-Model filterable facts (`fact+value → registered destination`); first-match-wins; matched-rule provenance; stale-field repair; equal-precedence → binder-repair artifact. Destinations = thread/artifact only (scope-inbox forward). | S1, S2 | Gmail `from=` registers+owns; repeat routes by ownership; GitHub/doc use same mechanics; equal-precedence→repair; removed field→stale. AC-1.6, AC-2.6. | Gmail/GitHub/doc need **connector-specific routing code**, or launch can't work without scope-inbox destinations. |
| **S4a** | S4 | Single `resolve_targets()` (registered-resolve · rule-bind+register · ownerless triage · dead-letter). **Migrates `route_event` onto it; deletes the GitHub-shaped branch.** | S1,S2,S3 | all 3 connectors traverse one resolve fn; archived triage re-resolves; mechanism fault ≠ clean. AC-2.*, AC-3.6/3.7. | Delivery needs a **user-facing class** (warning/decision/report/raise). |
| **S4b** | S4 | Disposition table keyed `(relationship, event_class, lifecycle)→wake\|digest`; `parent_chain` hard-wired digest; delivery-health owner chain + non-archivable workspace health sink; watch-expiry routed as delivery failure. | S4a | parent_chain never wakes; health failure→narrowest live sink; expiry→failure. AC-3.1–3.5, AC-2.7. | Health-owner routing can **dead-end silently**. |
| **S5a** | S5 | Artifact Review Profile execution (fresh: artifact snapshot + profile criteria/templates/reviewer context; **no parent convo context v1**); comment + structured recommendation; provider-neutral. | S2 (S4 for emit) | low-quality ask blocked by profile; fresh-context proven (no parent context). AC-4.10, AC-4.11. | Profiles **useless without parent conversation context**. |
| **S5b** | S5 | Parent disposition API — all 7 outcomes (`approve_to_user`, `reject_with_reason`, `answer_internally`, `request_revision`, `reroute_owner`, `defer_until`, `supersede`); route-back via Event Delivery; blocking-profile-failure prevents approve unless authorized override. | S5a, S4 | reject/answer/revision route back, never look user-delivered; blocking failure needs override. AC-3.9, AC-4.11. | Parent disposition **loses the child's original intent**. |
| **S6a** | S6 | Attention API: resolve raised artifacts; `POST attention-outcome{outcome,reason?,condition?}` with **closeout fan-out across all subscribed levels** (≠ archive, ≠ sub-archive); deadline `defer_until` arms an Automation watch. | S4b, S5b | closeout removes across subscribers without archiving; deadline defer arms+re-raises. AC-4.2–4.5, AC-4.12. | Closeout can't propagate without refresh or without mutating archive state. |
| **S6b** | S6 | Launch UI: requires-user artifact card (solid surface, row-per-request, persistent raised indicator, close≠clear); core-4 parent controls; pull-only parent roll-up (no badge/wake chaining); conductor-visible surface. 2 net-new primitives → `/_design` first. | S6a | close-viewer≠clear; roll-up no unread pressure; conductor surface scope-filtered. AC-4.1/4.7/4.8, B6.*. | Launch UI would force the user to track the worker/thread graph manually. |
| **S7** | S7 | Reliability automations on one runner: expected-event watches (arm/persist/expire-once/resolve); plan/thread liveness (waiting-on-watch vs idle-incomplete); startup re-arm + restart drain; observe-only automation-health surface. | S1, S4b | missing reviewer/CI→one routed wake; no double-wake on dup expiry; restart re-arms; incomplete-plan liveness wake. AC-5.*, AC-6.3. | A missing expected event can **look clean**, or automation health can go **dormant silently**. |

**Cross-slice gate (every checkpoint before promotion):** the 10-scenario cross-slice test pack (sequencing RFC §Cross-Slice Test Pack) must reference the named fixtures from the S0 checkpoint.

## Out of launch (do not build)

Broad user-authored automations · full conductor failover · scope-inbox rule destinations · composite/regex/preview binder · profile authoring/config UI · multi-profile mgmt · rich reroute/defer/supersede controls beyond API+deadline-defer · emergency direct-to-user bypass.
