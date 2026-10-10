---
name: consolidate-finding
description: Takes a duplication or shape-mismatch finding (from /code-cleanup-audit or named directly) and produces an executable consolidation plan — canonical pick, behavior-preservation matrix, callsite migration order, regression budget. Designed so a separate execution session can run the plan without rediscovering anything.
---

# Consolidate finding

Turns a "we have two of these" finding into a "merge them safely, in this order" plan.

The audit (`/code-cleanup-audit`) tells you *what* is duplicated. This skill tells you *how* to consolidate it without losing behavior or breaking callers. The output is a written plan that a separate session — possibly run from a fresh context — can execute end-to-end.

## When to invoke

```
/consolidate-finding                              # interactive — asks for inputs
/consolidate-finding <path-A> <path-B> [path-C]   # name the duplicate sites directly
/consolidate-finding --report=<audit-report-path> --finding=<finding-title-or-line>
/consolidate-finding "<symbolA in pathA>; <symbolB in pathB>"
```

Inputs the skill needs (will ask if missing):
- The 2+ duplicate sites (file:line or fully-qualified symbol).
- Whether the user already has a preference for which is canonical (optional).
- The repo's test command (optional — will probe `package.json`/`pyproject.toml` if not given).

## What this skill produces

A single markdown file at `./.consolidation/<ISO-date>-<short-name>.md` containing:

1. **Site inventory** — each duplicate, fully qualified.
2. **Behavior matrix** — per-site capabilities, side effects, error handling, return shape.
3. **Test coverage** — which tests cover each site and what they assert.
4. **Canonical pick** — which site survives, with explicit reasoning.
5. **Preservation list** — behaviors unique to non-canonical copies that must move into the canonical version.
6. **Callsite map** — every importer/caller of each non-canonical copy.
7. **Migration sequence** — ordered list of edits with rationale.
8. **Regression budget** — exact test commands that must pass before each step and at the end.
9. **Risk register** — what could break, how to detect early, what to roll back.

The output is the deliverable. A subsequent session reads the file and executes — no re-discovery.

## Operating rules (non-negotiable)

- **This skill plans; it does not consolidate.** Do not edit production code. Edits are reserved for the execution session that runs after the plan is approved.
- **Two copies are not the same just because they look the same.** Always read both bodies in full and write the behavior matrix before picking a canonical.
- **A passing test is not proof.** A function with no tests has no documented behavior, only de facto behavior. Treat untested copies with extra scrutiny.
- **Canonicalize on the most-tested, most-imported version unless there's a specific reason not to.** When choosing the less-imported version, name the reason explicitly in the plan (e.g., "site B is in the public package surface; site A is internal").
- **Preserve, don't widen.** If copy A handles edge case X and copy B doesn't, the merged version handles X. Never silently drop edge cases — surface every preservation as a checklist item.
- **Migrate callsites in dependency order: leaves first.** Files that other code imports come last. Never delete a non-canonical copy until all its callers point elsewhere AND the test suite passes.

## Step-by-step

### 1. Gather inputs

If invoked with explicit paths/symbols, use them. Otherwise ask the user:
- Which duplicate sites? (≥2 file:line or symbol references)
- Is there an audit report to reference? (`./.cleanup-audit/<date>-audit.md` is the convention)
- Any existing preference for canonical? (no preference is fine)
- What's the project's test command? (e.g., `npm test`, `pytest`, `cargo test`)

Confirm the inputs back to the user before proceeding. If a site is ambiguous (e.g., file path with no line number, multiple matching symbols), grep and pick — but show what was picked.

### 2. Build the behavior matrix

Read each copy in full. For each, extract:

- **Signature** — name, parameters with types, return type, exceptions raised.
- **Inputs accepted** — types, value ranges, edge cases handled (None, empty, negative, oversized, etc.).
- **Outputs produced** — exact shape and edge cases (does it return [] for missing? raise? null?).
- **Side effects** — I/O, mutation of inputs, mutation of module/global state, logging, metrics, audit trail emission.
- **Error handling** — what's caught, what's re-raised, what's silently swallowed.
- **Performance characteristics** — known O() if surfaced, hot-path or not.
- **Dependencies** — modules / external services / DB / config it touches.

Render as a table. Per-row aspect, columns = each copy. **Anywhere two copies disagree is a finding for the preservation list.**

### 3. Find every callsite

For each copy, find every importer and every dynamic-dispatch reference. Use parallel grep + entry-point checks:

```
grep -rn "<symbol>" --include="*.<ext>" .
grep -rn "from <module> import" .
grep -rn "<module>\.<symbol>" .
```

For each callsite, record: `path:line`, calling function, what it passes in, what it does with the result. This is the input to the migration sequence — the migration order is callsite-by-callsite.

If the symbol could match dynamically (string-based dispatch, getattr, plugin registries), grep for the *string* of the symbol name too. If any match, flag the consolidation as `MEDIUM-RISK` and note the dynamic call sites in the risk register.

### 4. Audit test coverage

For each copy, find the tests that exercise it:
- Direct tests (test calls the symbol).
- Indirect tests (test calls a higher-level function that transitively calls the symbol).

For each test, record what it asserts. The behavior-preservation list (step 5) is the *union* of every assertion the canonical version must satisfy.

If a copy has no tests, surface this prominently. Don't refuse to consolidate — but the plan must include a "characterization test" step before consolidation: write tests that lock in current behavior, then proceed.

### 5. Pick the canonical

Score each copy on:
- **Imports / call count** (more = stronger candidate).
- **Test coverage** (more = stronger candidate).
- **Public-surface status** (exported by a published package = strong "must keep" signal).
- **Code clarity** (fewer special cases = easier to extend later).
- **Owner / git-recency** (more recent activity often means more reasons it's load-bearing).

Pick one. Write a 2-3 sentence rationale that names which factors mattered. **The rationale is part of the plan — readers will second-guess it if it's missing.**

### 6. Preservation list

For each row in the behavior matrix where copies disagreed, decide whether the canonical version should:
- (a) **Adopt** the non-canonical behavior (because it handles a real case the canonical missed).
- (b) **Reject** it (because it's wrong / redundant / a workaround for a now-fixed bug).
- (c) **Defer** it (because the case is rare and adopting it changes the contract — flag for a follow-up).

For each (a), add a checklist item to the migration: "Update canonical to handle <case>, with test."
For each (b), name why explicitly so the executor doesn't re-add it under a different name.
For each (c), file a follow-up task and link it.

### 7. Migration sequence

Ordered, callsite-by-callsite. Default order:
1. **Add tests to canonical** for any preservation-list (a) items, before changing behavior.
2. **Implement preservation** in canonical. Run tests.
3. **Migrate callers** of non-canonical copies, leaf modules first (no inbound edges in the call graph). Per-call:
   - Change the import.
   - Change the call (signatures may differ — adapt at the call site rather than widening canonical).
   - Run the relevant tests for that file.
4. **Delete the non-canonical copies** once *every* caller has been migrated AND the full test suite passes.
5. **Final full-suite run.**

Each step in the sequence has: action, rationale, expected test outcome, rollback (typically `git checkout -- <file>` of that single change).

### 8. Regression budget

State the test commands explicitly:

```
# Per-file edits during migration:
<file-scoped test command, e.g., `pytest tests/test_X.py -x`>

# Final full-suite verification:
<full test command, e.g., `npm test` / `pytest -x`>
```

If the project has type-checking, lint, or build steps, include those:

```
# Type / lint / build gates:
<commands>
```

The plan has succeeded only when all of these pass at the end. If the project has flaky tests, name them — the executor needs to know what's noise.

### 9. Risk register

For each plausible failure mode:
- **Mode** — how this could break.
- **Detection** — what would surface the break and when in the sequence.
- **Mitigation** — what to do.
- **Rollback** — git ref to return to.

Common failure modes worth checking for every consolidation:
- Hidden caller via dynamic dispatch (covered in §3 if found).
- Signature drift — canonical and non-canonical disagree on parameters; some callers pass shape only one accepts.
- Default value drift — same parameter name, different defaults between copies; collapsing to one default changes behavior at sites that relied on the old one.
- Side-effect drift — one copy logs/audits, the other doesn't. Logging is observable; deleting it can violate compliance contracts.
- Closure / module-state capture — one copy uses module-level state; merging into a function in another module loses that capture.

## Output format

Save to `./.consolidation/<ISO-date>-<short-name>.md`. Suggested short-name: kebab-case the canonical symbol's name.

```markdown
# Consolidation plan — <symbol> (<ISO-date>)

## Sites
1. **<file:line>** — `<full-qualified-name>` — <one-line summary>
2. **<file:line>** — `<full-qualified-name>` — <one-line summary>
(more if applicable)

## Behavior matrix
| Aspect              | Site 1 | Site 2 | Disagreement? |
|---------------------|--------|--------|---------------|
| Signature           | ...    | ...    | yes/no        |
| Inputs accepted     | ...    | ...    | ...           |
| Outputs / errors    | ...    | ...    | ...           |
| Side effects        | ...    | ...    | ...           |
| Error handling      | ...    | ...    | ...           |
| Performance notes   | ...    | ...    | ...           |
| Dependencies        | ...    | ...    | ...           |

## Test coverage
- **Site 1** — covered by: <test files>; assertions: <summary>; coverage estimate: <high/medium/low/none>.
- **Site 2** — ...

## Canonical pick
**Site N** survives. Rationale: <2-3 sentences naming the factors>.

## Preservation list
- [ ] (a) Site 2 handles <case> — adopt into canonical. Test: <test name>.
- [ ] (b) Site 1 has <pattern> — reject in merged version. Reason: <one line>.
- [ ] (c) Site 2 has <rare case> — defer; file follow-up <ID>.

## Callsite map
### Calling site 1 (<count> callers)
- `<path:line>` in `<calling-fn>` — passes <args>; uses result for <purpose>.
- ...

### Calling site 2 (<count> callers)
- ...

## Migration sequence
1. Add tests to canonical for preservation items (a). Run: `<file-scoped test cmd>`.
2. Implement preservation. Run: `<file-scoped test cmd>`.
3. Migrate callers in order:
   3a. <path> — <action>. Run: `<test cmd>`.
   3b. <path> — <action>. Run: `<test cmd>`.
   ...
4. Delete non-canonical copies at <path> and <path>. Run: `<full suite>`.
5. Final verification: `<full suite>` + `<typecheck>` + `<lint>` + `<build>`.

## Regression budget
- File-scoped: `<cmd>`
- Full-suite: `<cmd>`
- Typecheck / lint / build (if applicable): `<cmds>`
- Known flaky tests to ignore: <list or "none">.

## Risk register
| Mode | Detection | Mitigation | Rollback |
|------|-----------|------------|----------|
| ...  | ...       | ...        | git ref  |

## Approval

This plan is **a plan, not a change.** A separate session executes it.
- Approved by: <user>
- Approved at: <timestamp on confirmation>
```

## Hand-off to execution

After saving the plan, report:
- The plan file path.
- A 100-word summary: which sites, the canonical pick + reason, total migration steps, total callsites to update, top risk.
- The exact command / prompt to start the execution session.

Suggested execution prompt template:
```
Execute the consolidation plan at <path>. Read the plan in full first.
Follow the migration sequence step by step. At each step, run the named
test command and verify it passes before moving to the next step.
If any step fails, stop and report — do not attempt a fix without
re-running the plan through /consolidate-finding.
```

## What this skill is NOT

- Not a refactor planner for arbitrary cleanup. It's specifically for "two-or-more copies → one copy".
- Not a fix for fundamentally broken duplications (e.g., copies that disagree on the *meaning* of the function — those need design review, not consolidation).
- Not for cross-package or cross-repo consolidation — assumes a single repo / single test command.
- Not for code that's duplicated *intentionally* (e.g., pinned compatibility shims, security-isolated implementations) — if the user's input names this, return early with "this is intentional duplication; not a consolidation candidate."
