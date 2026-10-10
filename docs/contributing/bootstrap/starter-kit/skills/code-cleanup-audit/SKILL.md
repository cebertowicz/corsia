---
name: code-cleanup-audit
description: Runs a comprehensive audit of a codebase looking for mechanical cruft (duplicates, dead code, type drift, cycles, weak types, silent errors, slop, code-health budgets) and — critically — shape-vs-function mismatches that mislead future contributors (especially LLM agents). Reports findings with confidence tiers; implements only high-confidence, zero-risk removals.
allowed-tools: Read, Bash, Grep, Glob, Edit, Write, Agent
---

# Code Cleanup Audit

A structured audit pass that finds and, where safe, removes three categories of cruft:

1. **Mechanical cruft** — duplicated logic, orphaned code, drifted types, circular imports, silent error-swallowing, slop and comment scar tissue. Found via seven focused tracks (adapted from `atj393/Codex-plugin-code-cleanup`, with comment hygiene strengthened to remove the gap that AI-assisted edits created).

2. **Code-health budgets** — files, functions, and identifiers that are too large or too cryptic for fast reasoning. Especially relevant for codebases that need to be readable by smaller local LLMs (Qwen 3.6 / 35B-class) where context window is tight. Found via track 9.

3. **Shape-vs-function mismatch** — code whose structure overstates what it actually does. The LLM-trap lens: patterns that look intentional but are historical residue, which new contributors (human or agent) preserve by default and extend incorrectly. Found via track 8.

Track 8 is the differentiator versus generic cleanup tools — it catches "what looks intentional but actively misleads." Track 9 is the local-LLM lens — it catches "what exhausts the reader."

## When invoked

Invoke with `/code-cleanup-audit` followed by optional paths.

```
/code-cleanup-audit                          # scan entire cwd
/code-cleanup-audit backend/ frontend/       # scan specific paths
/code-cleanup-audit backend "focus on types + dead code"    # with steering
```

## Core principle (read before doing anything else)

**The single question behind every finding: "Does this code's shape accurately reflect what it does, or does the shape overstate, under-state, or misrepresent the function?"**

All eight tracks are different lenses on this question. Keep the question in mind while running each track; findings that answer "no, the shape lies" are the ones that matter. Findings that are just "this could be written more tersely" are noise — don't surface them.

## Execution plan

Run the nine tracks as a coordinated audit. Launch tracks 1–7 and track 9 as parallel `Agent` invocations (each receives its playbook and target paths); run track 8 (shape-vs-function) **after** the others complete so it has their findings as input — track 8's review consolidates patterns across the others. Then run the review step.

### Nine tracks

| # | Track | Playbook | What it finds |
|---|---|---|---|
| 1 | dedup | `playbooks/dedup.md` | Repeated logic, copy-pasted functions, redundant abstractions |
| 2 | types | `playbooks/types.md` | Duplicated shared types that drifted across files |
| 3 | dead-code | `playbooks/dead-code.md` | Unused exports, orphaned files, unreferenced functions (manually verified) |
| 4 | cycles | `playbooks/cycles.md` | Circular imports that hurt maintainability or testability |
| 5 | strong-types | `playbooks/strong-types.md` | `any`/`unknown`/untyped placeholders where the real type is knowable |
| 6 | errors | `playbooks/errors.md` | Silent `catch`, swallowed failures, misleading defaults, broad exception hiding |
| 7 | slop | `playbooks/slop.md` | **Comment hygiene** (orphans, lying, narrating, tautology, low-value), placeholder stubs, deprecated paths, redundant re-exports |
| 8 | **shape-mismatch** | `playbooks/shape-vs-function.md` | **Sentinels, speculative abstractions, one-impl protocols, capability-lying booleans, global leniency flags, fossil surface, misleading names** |
| 9 | **code-health** | `playbooks/code-health.md` | **Oversized files/functions, deep nesting, high complexity, missing public docstrings, identifier hygiene, single-file token-budget** |

### Review step

After the eight tracks report, run `playbooks/review.md`. The reviewer audits the aggregated findings and any proposed diffs as a skeptical senior engineer. Its job is to:

- Catch findings where the track was over-eager (e.g., "dead code" that's a public export used by a dependent project).
- Apply the shape-vs-function lens holistically across the whole finding set — sometimes what looks like three separate findings is one systemic issue.
- Promote or demote confidence tiers.
- Assemble the final report.

## Operating rules (non-negotiable)

These apply to the lead orchestrator and every track agent. They exist because a cleanup pass that breaks the build is worse than no pass at all.

- **Be conservative. Preserve behavior exactly.** Any change that could alter runtime behavior is reported, not implemented — even if "obviously safe."
- **Diffs stay small and reviewable.** Prefer multiple single-concern diffs over one big one.
- **Only implement HIGH-confidence, LOW-risk changes.** Everything else is reported for human decision.
- **Confidence tiers:**
  - `HIGH` — zero consumers (truly dead), evidence from full-repo grep. Safe to remove.
  - `MEDIUM` — Likely cruft but has callers; judgment needed. Report only.
  - `LOW` — Might be cruft; lacks evidence. Report only if the pattern is distinctive enough to warrant a look.
- **Never claim success without naming the checks actually run.** If you didn't run the test suite, say so. If you ran `grep` but not LSP, say so.
- **Parallel assessment is encouraged; parallel editing is not.** Only one agent writes to a given file per run.
- **Medium-confidence items are findings, not fixes.** Do not implement and flag; report only.

## Output format

Save the report to `./.cleanup-audit/<ISO-date>-audit.md` (create directory if needed). Structure:

```markdown
# Code Cleanup Audit — <date>

## Summary
<One paragraph: total findings by track, total implemented, total reported.>

## Implemented changes (HIGH confidence, LOW risk)
<Brief list of what the audit changed, with file:line and why.>

## Findings to review
### Track: shape-mismatch
#### [MEDIUM] <finding title>
- **File:** <path:line>
- **What it looks like:** <description of the shape>
- **What it actually does:** <description of the function>
- **Why the mismatch matters:** <how it misleads>
- **Suggested fix:** <direction only, don't write code>
- **Evidence:** <grep results, consumer count, commit reference>

### Track: dedup
... (similar structure per track)

## Explicit non-findings
<Patterns the audit considered but decided are load-bearing. This is as important as the findings — prevents re-checking the same thing.>

## Calibration notes
<Any cases where the audit's confidence was hard to set — flag for the human to adjust the playbook.>
```

## Iteration

This skill is designed to be run repeatedly. Each run should produce fewer findings than the last on the same codebase — if it doesn't, the playbooks need refinement. Flag calibration cases explicitly so the playbooks improve over time.

## Getting started

Read `playbooks/shape-vs-function.md` and `playbooks/code-health.md` first — track 8 is the LLM-misleading lens, track 9 is the LLM-exhausting lens. Together they're the local-LLM-friendliness pair. The other seven adapt well-known patterns; tracks 8 and 9 are where the skill earns its keep on a codebase that needs to be readable by smaller local models.

After reading the playbooks, spawn tracks 1–7 and 9 in parallel via `Agent`, then track 8 (shape-vs-function) sequentially (it needs the others' output), then the review step. Report back with the path to the saved audit report plus a <200-word executive summary of the highest-impact findings.
