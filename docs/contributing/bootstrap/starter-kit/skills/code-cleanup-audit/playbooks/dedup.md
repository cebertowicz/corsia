# Playbook: dedup

Find and remove duplicate logic, copy-pasted functions, and redundant abstractions. Distinguish *accidental* duplication (same code, same meaning) from *intentional* duplication (same code, different domain).

## What to look for

- Functions with near-identical bodies in different modules.
- Helper functions that reimplement a stdlib or well-known utility.
- Multiple implementations of the same normalization (string cleaning, path handling, type coercion) across the codebase.
- Adjacent if/elif branches doing structurally identical work with small variations.
- "Helper" classes that wrap a single stdlib call.

## Evidence to collect

For each duplication candidate:
- The file:line of each copy.
- A diff showing where the bodies converge and diverge.
- Call sites for each copy.
- Whether the copies have the same domain meaning or happen to have the same shape.

## Rules

- **Before merging two functions, confirm they mean the same thing, not just look the same.** Two auth validators both named `check_token` might have subtly different semantics; merging them is a correctness bug.
- If one copy has test coverage and the other doesn't, keep the tested one.
- Domain-adjacent helpers (e.g., `slugify` in `blog/` and `slugify` in `files/`) may need to stay separate if they're deliberately independent — flag but don't merge automatically.

## Confidence tiers

- `HIGH`: Two call sites importing from two places, identical bodies, identical domain. Safe to merge.
- `MEDIUM`: Similar bodies, similar domain. Report with suggested merge target.
- `LOW`: Structural similarity only, different domains. Report only if the pattern is distinctive enough that a reader would assume they're related.

## What NOT to flag

- Tests with similar structure but different assertions.
- Protocol/interface method signatures repeated at each implementation.
- Copy-pasted code that's actually correct for its context and would become coupled via a bad abstraction.

## Output

Report each duplication cluster with:
- The N files involved.
- The common body (show it once).
- The variant details per site.
- Suggested fix direction (extract to `utils/X.py`, inline the single caller, etc.).

## Hand-off to consolidation

For any `MEDIUM` or `HIGH` dedup finding the audit reports (rather than implements), the recommended next step is the `/consolidate-finding` skill. That skill takes the finding and produces a full consolidation plan: canonical pick, behavior matrix, preservation list, callsite migration sequence, regression budget. It's the bridge between "the audit found duplicates" and "a separate session can safely merge them."

The audit deliberately stops at the dedup boundary — implementing a merge requires reading both bodies in full, mapping callsites, and judging which behaviors must survive. That's `/consolidate-finding`'s job, not this track's.
