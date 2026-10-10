# consolidate-finding

A planning skill for safe consolidation of duplicate code. Pairs with `/code-cleanup-audit`.

## What it does

Takes a duplication finding (from an audit report or named directly) and produces an executable consolidation plan, saved to `./.consolidation/<ISO-date>-<short-name>.md`.

The plan answers everything a downstream execution session needs:

1. **Site inventory** — exactly which copies are being consolidated.
2. **Behavior matrix** — per-copy capabilities, side effects, error handling, dependencies.
3. **Test coverage** — what's covered, what isn't.
4. **Canonical pick** — which copy survives, with explicit reasoning.
5. **Preservation list** — unique behaviors from non-canonical copies that must move into the canonical version.
6. **Callsite map** — every importer/caller of each non-canonical copy.
7. **Migration sequence** — ordered, callsite-by-callsite, leaves first.
8. **Regression budget** — exact test commands the migration must pass.
9. **Risk register** — what could break, how to detect, how to roll back.

## Why this exists

`/code-cleanup-audit` finds duplicates well. But its output stops at "suggest extracting to `utils/X.py`" — which gives a downstream session enough rope to break things. Specifically:

- Which copy is canonical, and why?
- What unique behavior in each copy must be preserved?
- In what order should callsites be migrated?
- What tests guard each step?

This skill answers those before any code changes. The user's complaint that "executors don't have enough direction" is the gap this skill fills.

## Invocation

```
/consolidate-finding                                          # interactive
/consolidate-finding <path-A> <path-B> [path-C]               # name sites directly
/consolidate-finding --report=<audit-path> --finding=<title>  # from audit report
/consolidate-finding "<symA in pathA>; <symB in pathB>"       # symbolic
```

## Plan, don't execute

This skill **plans only.** It does not modify production code. A separate execution session reads the saved plan and runs it. That separation matters — planning needs a critical eye on what *might* break; execution needs disciplined step-by-step verification. Mixing them is how bugs ship.

## Layout

```
consolidate-finding/
├── SKILL.md      — orchestrator prompt (loaded on invocation)
└── README.md     — this file
```

## Output location

Plans land at `./.consolidation/<ISO-date>-<short-name>.md`. The directory is created if missing. Suggested gitignore entry: `.consolidation/` (treat plans as throwaway artifacts; commit any plan you actually want to keep).
