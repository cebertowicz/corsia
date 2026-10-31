# Engineering & Architecture Standards

**Status:** current · **Owner:** CORSIA maintainers

**What this is:** CORSIA's durable **engineering contract** — what every code change + RFC is reviewed against (the Eng/Arch gate lens). Workspace config: shipped default, per-project overridable. The negative twin — specific failures seen in the wild — is [llm-anti-patterns.md](llm-anti-patterns.md); **this doc is the affirmative law.**

**The bar:** code a **staff engineer passes without a sigh** — one obvious path, no hidden state, no parallel structures, and **no name that lies.** The test for every rule: does it keep the system simple, legible, single-pathed, and *true to itself* as it grows — or does it add debt?

---

## 1 · One path
- **EAS1.** **Replace, don't preserve.** One path for a thing. No migration bridge, fallback, compat shim, "mirror," or "old way + new way" parallel structure — *ever*, without explicit approval **and** a committed removal step. When a change breaks old data/behavior, **migrate** it; don't keep the old path alive. `[invariant]`
- **EAS2.** **One canonical source of truth per concept.** Derived/cached state is subordinate, never authoritative. No dual-write; no second copy of a concept that can disagree with the first.
- **EAS3.** **Extend before you fork.** Search for prior work first; extend an existing primitive/endpoint/table as a thin caller. A parallel path duplicating an existing one is the defect, not the feature.

## 2 · Abstractions & names tell the truth
- **EAS4.** **Every abstraction sits at one clear level and does one thing.** No blurry, catch-all, or wrong-altitude abstractions — they are exactly where LLMs and humans silently diverge on what something *is*.
- **EAS5.** **Names match ground truth.** A function / method / class / table / event is named for what it actually is and does. A name that no longer matches its meaning is a bug — **rename it to match, immediately; never defer.** Software evolves; the name evolves with it.
- **EAS6.** **Forks serving the same purpose stay congruent** — two paths doing the same job share shape, vocabulary, and naming; they don't drift into dialects.
- **EAS36.** **One product name, cased by kind.** The product is **CORSIA** in prose, UI text, docs, comments, and messages; **`corsia`** in every identifier — repository, paths, packages, modules, commands, keys (frontmatter included), labels, URLs, and tool prefixes; **`CORSIA_`** prefixes environment variables. "Corsia" is never correct. The former product name (`maestro`, in any casing) appears in no new text, alias, dual key, or read-side fallback; stored data that still carries it is converted once (EAS1, EAS8). Only a literal historical reference to the former repository names it. `[invariant]`
- **EAS34.** **Conceptual complexity must earn its keep.** Split concepts only when ownership, authority, behavior, or lifecycle truly differs. High-overlap concepts, repeated safety catches, exception chains, and code/model abstraction divergence are redesign triggers, not polish. A "belt and suspenders" guard can be correct once; repeated guards usually mean the model is unreliable. A change that adds a mechanism, concept, state owner, or path (Development Doctrine categories 4–5) carries a **complexity ledger** in its work object: what it adds, what it removes or collapses in the same change, which ledger (internal vs user/agent) each side sits on, and one falsification sentence stated before the work. A future removal is a follow-up node, never a credit. `[invariant]`

## 3 · Root cause — and finish the job
- **EAS7.** **Fix the *deepest correct* locality, not the nearest plausible one.** A fix that "looks like the long-term solution" but sits at the wrong layer is still a band-aid. Trace to where the problem is actually born.
- **EAS8.** **The fix isn't done until the symptom is gone.** Fixing the source without cleaning up what it already produced is half a fix — **migrate the prior data and reconcile the upstream symptom in the same change.** `[invariant]`
- **EAS9.** **Never "fix it, then route around the fix."** Leaving a backward-compat path that serves the old shape *alongside* the correct one is doing the right fix and choosing not to use it. Migrate to the one correct path (ties to EAS1). `[invariant]`

## 4 · No hidden state
- **EAS10.** **Config-first for swappable policy.** No hardcoded model names, deployment/user paths, IDs, or operator-supported product defaults — anything operators or users are meant to swap is configuration, resolved explicitly or it fails loud. Named, tested internal algorithmic bounds (including safety timeouts) stay in code when they are part of correctness rather than an exposed policy; inventing a config knob for every constant violates EAS22. `[invariant]`
- **EAS11.** **Loud failure, always.** No silent fallback, no swallowed exception, no `|| true`. A failure errors with what went wrong; a thing that can't be done says so, loudly. `[invariant]`
- **EAS12.** **No single-tenant assumptions** in repo artifacts — name the role (`user` / `owner`), never a specific person; no environment baked into code.

## 5 · Correct under change & concurrency
- **EAS13.** **Operations on shared/persisted state are atomic and idempotent** — no read-then-write race (TOCTOU), no partial-state corruption; a retried operation is safe to run twice.
- **EAS14.** **Don't silently break a consumed contract.** An API surface, typed event, or message shape something else depends on is changed compatibly or versioned — never broken out from under its consumer. This requires evidence of a remaining consumer or documented external contract; an explicitly retired internal shape whose known callers are co-migrated in the same change does not require a dead compatibility shell.
- **EAS15.** **Tolerate old + new shapes across a rollout** at any boundary that can see both (a payload, a row, an event) — until the migration that retires the old shape lands.
- **EAS16.** **Migrations are idempotent, reversible, and non-destructive** — they backfill, they don't drop-without-migrate, and re-running one is a no-op.
- **EAS35.** **One owner per state machine.** Stateful behavior names one canonical owner plus the allowed transition/failure matrix before implementation. Multiple registries, flags, callbacks, or repair loops claiming the same lifecycle are a redesign trigger.

## 6 · Cache & context discipline (CORSIA-specific — breaks subtly)
- **EAS17.** **KV strict-prefix is sacred.** Per-turn-variable context and any attribution that reaches the *tokenized* prompt is frozen into message content at write time, never re-rendered per build; display-only data stays in metadata. `[invariant]`
- **EAS18.** **Verify the boundary per provider/adapter** — native completion ≠ CC/Codex transcript ≠ the next adapter. Never assume one cache/context model.

## 7 · Secure, diagnosable, bounded
- **EAS19.** **Authorize every access; trust nothing.** Ownership/permission checked on every read+write of user data; **least privilege** (grant the minimum; an agent's actions are sandboxed / path-gated, never ambient); no secrets in code or logs; validate untrusted input at the boundary.
- **EAS20.** **Diagnosable without a repro — to a standard pattern.** Logs follow **one consistent structured format** (level · component · event · context · error) so the stream is **machine-reviewable** — an error carries enough context to find the cause without reproducing it. Traces at the seams; loud *and* legible.
- **EAS21.** **Bounded by default.** No unbounded query, loop, or growth; paginate; **no N+1 — batch it**; respect rate limits; know the cost of a hot path; **never block the request/event loop — offload blocking work.** A change that adds or changes a read on a request path (a page-load or API route handler and anything it calls) states its **scale budget in the PR body**: queries per request and worst-case rows read for the largest parent at current production volume and at 10x. A relation with no known production volume is budgeted at its table's total row count. A request-path write states the statements run inside its write transaction and keeps compute outside `BEGIN IMMEDIATE`.
- **EAS32.** **The workspace is the user's whole context — guard it accordingly.** It accumulates everything about a user's projects *and* the user themselves, which makes **scope control and data safety paramount**: context is **scoped** (a project's data never leaks across scope; personal context is access-gated to its owner), **protected** (guarded at rest, never logged, never embedded in a prompt outside its scope), and **never exfiltrated** — egress is the user's explicit, auditable choice. `[invariant]`
- **EAS33.** **Every capability ships instrumented.** It emits the product events that make its use, health, and value measurable from day one — a capability you can't measure is one you can't improve or prove. Product events are the product-health contract; traces/OpenTelemetry can support diagnostics, but do not replace product semantics.

## 8 · Simple & legible
- **EAS22.** **Ship the simplest correct fix.** If 200 lines could be 50, write 50. No speculative flexibility; no abstraction for single-use code.
- **EAS23.** **No *unrelated* churn.** Don't reformat, rename, or refactor what the task doesn't require — that's blast radius you didn't need. *(A correctness fix in the path you're already touching, or a deliberate refactor on its own branch, is **not** this — it's welcome. What this kills is drive-by noise that hides the real change.)*
- **EAS24.** **Stay within health budgets:** files small (Python/Svelte ≤400, TS ≤300 LOC), functions ≤80 LOC, nesting ≤4, complexity <15 — over budget → split before adding. Explicit over clever; flat over nested; public functions documented.

## 9 · Proof & integration discipline
- **EAS25.** **Tests are not optional.** Integration coverage precedes the change; a failing test is fixed, never suppressed.
- **EAS26.** **Coherence + green are mandated only at the `dev` / `main` merge.** A multi-stage refactor lives on its own branch and **need not** be coherent or green at each intermediate commit — don't burn time/tokens making every WIP commit pass; reconcile at the final PR.
- **EAS27.** **Nothing merges to `dev` / `main` without a review.** Branches are free; an unreviewed merge is the expensive thing.

## 10 · Data models the truth & stays clean
- **EAS28.** The **data model mirrors the domain** — tables / columns / relations named and shaped for what they actually represent (EAS5's naming discipline, applied to data). A column means one thing.
- **EAS29.** **Referential integrity is enforced** — no orphaned rows, no dangling references; deleting a parent never leaves ghost children.
- **EAS30.** **Every row class has a lifecycle** — a defined retention/cleanup; no unbounded accumulation of stale or dead rows.
- **EAS31.** **One row, one truth; deletes & archives are real.** Unique constraints prevent duplicate/conflicting rows for an entity; archived/tombstoned data is **excluded from active queries and actually clears** — no soft-delete cruft silently polluting results. `[invariant]`

---

## Reviewer Contract
**Scope ceiling:** a finding may block the current diff or request explicit re-scope. It never authorizes adjacent implementation beyond the current user-approved scope/PRD. Review the whole blast radius; prescribe only what the current change is authorized to deliver.

**Review-process discipline:** catch problems; do not solve them. A finding states the defect, concrete evidence, and consequence, but never proposes a remedy or requests new machinery. Flags and nits are diagnostic evidence, not patch instructions. A linked follow-up issue is an acceptable merge disposition for a Flag or Nit; reserve Block for a consequence that makes the current revision unsafe to merge and cannot be deferred. Filing an issue never silently relabels a true Block. Before claiming a capability or owner is missing, search the exact reviewed tree for a semantically equivalent owner, contract, helper, or state path; conceptual duplication cites both locations and the conflicting truth it permits.

Evaluate all findings together in the same pass. Emit one PR-level `review-process/approach-churn` Block immediately — including on the first review — when correlated defects share a design root, local fixes are creating support machinery or secondary defects, or central/repeated conceptual duplication shows the approach is wrong. Describe the pattern and consequence, not a redesign. Prior findings are trajectory evidence, never instructions. Round count alone is never a threshold: independent localized defects may legitimately take more rounds, and an explicitly authorized rerun simply reviews the new revision under the same contract.

- **Block:** a parallel/fallback/compat path without approval (EAS1/EAS9) · hardcoded swappable operator/product policy (EAS10) · a silent fallback / swallowed failure (EAS11) · a cache-prefix violation (EAS17) · a duplicate concept / second source of truth (EAS2) · a name that no longer matches its meaning, left unchanged (EAS5) · the former product name or a product-name casing that breaks EAS36 · a source-fix with no migration for prior data (EAS8) · a silently-broken consumed contract with a concrete remaining consumer or documented external contract (EAS14; co-migrated internal retirement is not a break) · an unauthorized data access (EAS19) · archived data still hitting active queries, or an orphaned row (EAS31/EAS29) · cross-scope leakage or unconsented exfiltration of workspace context (EAS32) · a doctrine category-4/5 change with no complexity ledger in its linked work object, or whose only credit is a future removal (EAS34) · a request-path read added or changed with no scale budget in the PR body, a budget with no numbers, or an `n/a` reason the diff contradicts (EAS21) · a suppressed test.
- **Flag:** a fork that should've extended (EAS3) · forks-for-the-same-purpose diverging (EAS6) · a split concept whose ownership/behavior difference is not clear (EAS34) · stateful behavior without one owner and a transition/failure matrix (EAS35) · repeated guard/catch/exception chains that suggest the model is unreliable (EAS34) · code abstractions diverging from the product/domain model (EAS34) · a fix at the wrong locality (EAS7) · a non-idempotent op on shared state (EAS13) · an over-budget file grown further · unrelated drive-by churn (EAS23) · an unbounded query/loop (EAS21) · a row class with no lifecycle / unbounded stale growth (EAS30) · a capability shipped without product-event instrumentation (EAS33) · a complexity ledger whose falsification sentence is missing or cannot be checked at merge (EAS34).
- **Nit:** style, micro-naming.

> The negative twin — specific observed failures — is [llm-anti-patterns.md](llm-anti-patterns.md). This is the affirmative law; that's the case law. 36 obligations, 10 themes.
