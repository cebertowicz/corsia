# Playbook: slop & comment hygiene

Find residue that LLM agents leave behind: orphaned comments, comments that lie about the code, low-value narration, stale stubs, and the slow accumulation of "comment scar tissue" that shrinks the effective context budget for the next agent.

This is the single most LLM-pollution-relevant track. A wrong comment is worse than no comment — it tells the next reader (human or model) a story the code no longer obeys, and an LLM will pattern-match on it and extend the lie.

## The one question for every comment

Ask, in order:

1. **Does the comment match the code below it?** If the comment describes behavior the code no longer (or never did) implement, it's a *lie*. Lies are HIGH-confidence removals.
2. **Does the comment reference symbols, paths, or concepts that still exist?** If it points at something that's gone, it's an *orphan*. HIGH-confidence remove.
3. **Does the comment add information not already derivable from** (a) the next 5 lines of code, (b) the function/variable/type names, (c) the type signatures, or (d) the surrounding test names? If no — it's *low-value*. Remove.
4. **Does the comment explain a non-obvious WHY** — a hidden constraint, a workaround for a real bug, a domain rule that doesn't fit in a name, an API contract for an external consumer? If yes — **keep**. These are the comments that earn their tokens.

Question 1 catches lies. Question 2 catches orphans. Question 3 catches noise. Question 4 is the saver — apply it carefully, because a sharp pruning pass that removes useful comments is worse than no pass at all.

## Categories (with confidence tiers)

### A. Orphan reference — HIGH

Comment names a symbol, file, or path that no longer exists.

- `// fall through to handleLegacyAuth` — but `handleLegacyAuth` is not defined anywhere in the file or imported.
- `# see docs in ./old_module.py` — but `old_module.py` doesn't exist.
- `// TODO(@alice): switch to fooBar()` — `fooBar` doesn't exist (and never did, or was removed).

**Detection:** parse the comment, extract identifier-shaped tokens (camelCase, snake_case, paths, ticket-style refs); grep the repo. Zero matches → orphan.

**Action:** remove. HIGH confidence as long as the named symbol genuinely cannot be found anywhere in the repo (not just the file).

### B. Lying comment — HIGH

The comment makes a factual claim about the next statement(s) that the code does not honor.

- `// skip auth in dev mode` followed by code that doesn't gate on `dev`.
- `# returns None on error` above a function that throws.
- `// idempotent — safe to call twice` on a function that has stateful side effects.

**Detection:** for short comments adjacent to a single statement, check whether the statement's behavior matches the comment's claim. Easier signal: comments containing words like `skip`, `only when`, `returns`, `throws`, `idempotent`, `safe`, `noop` — verify against the next statement.

**Action:** remove the comment (HIGH) OR fix the code (out of scope for the audit — report as MEDIUM with the discrepancy noted). Default to removing the comment because it's the truthful change; the code is the source of truth.

### C. Narrating prior edits — HIGH (no age gate)

Comment explains what the code *used to* do or why something was removed/changed.

- `// was using LRU cache, removed because of memory pressure`
- `# previously this called fooBar() — replaced inline`
- `// removed retry loop in v3`
- `/* legacy path — kept for backwards compat with v0 clients (none remaining) */`

These belong in commit messages and PR descriptions. They have **zero value at the call site** because they describe absent code; readers can't verify them and shouldn't try.

**Detection:** keywords — `was`, `previously`, `used to`, `removed`, `temporarily`, `backwards compat`, `legacy`, `kept for`. Combined with no surrounding code matching the narrated past, these are orphans by another name.

**Action:** remove. HIGH confidence regardless of git age — AI-induced narrating cruft is *born stale*. The `>3 months` heuristic that older versions of this playbook used is wrong: the worst offenders are the ones added in the same edit that removed what they describe.

### D. Tautological / restates the code — HIGH

Comment says what the next line obviously does.

- `// increment counter` above `counter++`
- `# return the user` above `return user`
- `// loop through items` above `for item in items:`
- `/** Get the name */` JSDoc above `getName()`

**Detection:** comment content paraphrases the immediate next statement using the same identifiers. Tooling check: tokenize comment, tokenize next statement, count overlapping non-stopword tokens; high overlap + comment ≤ 1 line → tautology.

**Action:** remove. These cost context budget for zero information.

### E. Verbose AI-style docstring on trivial function — MEDIUM

Multi-line docstring describing a function whose name + signature already fully convey the contract.

- `def double(x: int) -> int:` followed by `"""This function takes an integer and returns the integer multiplied by two."""`
- A 6-line JSDoc on a one-line setter.

**Heuristic:** docstring length > 2x function body length, AND function body is ≤ 3 lines, AND function does not raise / mutate state / call I/O.

**Action:** report as MEDIUM with a suggested 1-line replacement. Don't auto-remove — some of these contain legitimate edge-case notes despite being verbose.

### F. Commented-out code — HIGH (if older than 30 days) / MEDIUM (recent)

Block of code surrounded by comment markers, no longer executed.

- `// TODO: re-enable this when X / function() { ... }`
- Inline `# old_call()` above the new call.

**Action:** if the commented block is older than 30 days OR the surrounding code has been substantially rewritten since: HIGH, remove. Recent commented-out code may be in-flight work — report as MEDIUM with a note to confirm with the author.

### G. Stale TODO / FIXME / XXX — MEDIUM

`TODO` without an assignee, ticket link, or concrete next step. Age is a signal but not the only one — a 1-week-old `TODO: fix this` with no owner is just as useless as a 6-month-old one.

**Action:** report as MEDIUM. Either convert to an issue with a real description or remove. Don't auto-remove — some of these mark real risks the author wanted future eyes on.

### H. Placeholder stubs in production paths — HIGH severity, MEDIUM action

`raise NotImplementedError`, `pass  # TODO`, `throw new Error("not implemented")` reachable from a production code path.

**Action:** flag prominently. Don't auto-fix (a stub is a real bug, not a comment). MEDIUM in terms of the audit's editing posture — HIGH in terms of how loudly the report should call it out.

### I. Redundant re-exports / "may need later" imports — HIGH

- `from .core import X; from .core import X as X` — stuttering re-export.
- `# import foo  # may need later` — speculative dead import.

**Action:** remove. HIGH confidence; both are noise with a long shelf life.

### J. Deprecated functions still in use — MEDIUM

Function carries a `# DEPRECATED: use Y instead` notice but has 5+ active callers and no migration plan in the codebase.

**Action:** report as MEDIUM. Either the deprecation is real (and needs a migration sketch) or the notice is wrong (and should be removed). Don't decide automatically.

## Explicit KEEP rubric

The following are **always keepable** — do not flag them, even if they look verbose.

- **Hidden constraint:** `// must run before initDB() — depends on env vars set there`
- **Workaround with anchor:** `// Bug 4421: Apple Silicon stalls on >256 KiB writes; chunk smaller. Re-test on macOS 16.`
- **Domain rule that doesn't fit a name:** `# Medicaid claims must be submitted before 90 days post-encounter — see CFR §447.45`
- **External API contract:** `/** Returns ISO-8601 timestamps. Do not change format — Stripe webhook depends on this. */`
- **Surprising behavior callout:** `// NB: this function returns *nullable* despite the name — see #2871`
- **TODO with assignee + ticket + concrete action:** `// TODO(@bob, INGEST-419): swap to streaming once we land batch v2`
- **Type-stub `...`** in `*.pyi` files (legitimate placeholder syntax)
- **Hand-authored API docs** intended for external library consumers (when the project is a published package)

If a comment provides *any* of: a why, a constraint, a non-obvious invariant, a hazard, a versioned external contract — keep it. The cost of a useful comment is small; the cost of removing one is unbounded for whoever next has to figure out what the code is doing.

## Evidence to collect

Per finding:
- File:line.
- The exact text of the comment (verbatim, including delimiters).
- Category (A through J).
- For orphans: the symbol that was searched and the grep result.
- For lying comments: the comment's claim and what the next statement actually does.
- For tautologies: token overlap percentage with the next statement.
- For commented-out code: git age of the block.
- For low-value: the rationale for why the comment fails questions 1-3 of the one-question test.

## Confidence tiers (consolidated)

- `HIGH` — A (orphan), B (lying), C (narrating prior edits), D (tautology), F-old (commented-out >30d), I (redundant exports / "may need later" imports). Auto-removable; runtime behavior unchanged.
- `MEDIUM` — E (verbose AI docstring), F-recent (commented-out, recent), G (stale TODO), H (production stub — high severity, but action is human-judgment), J (deprecated-with-callers).
- `LOW` — Surface signals only. Report a pattern if multiple instances cluster in one file (signals a stylistic issue worth a note); don't flag individual occurrences.

## What NOT to flag

- Comments that pass any of the KEEP rubric clauses above. Bias toward keeping when in doubt.
- Long comments on intentionally-complex code (regex, bit-twiddling, math) where the explanation is genuinely the bridge between intent and code.
- License headers and copyright notices.
- Type-stub files (`*.pyi`) using `...`.
- Pragmas / linter directives (`# noqa`, `// eslint-disable`, `# type: ignore`) — these aren't prose comments.
- Generated-file headers (`/* DO NOT EDIT — generated by ... */`).

## Cross-reference with shape-vs-function

Misleading **names** (function/parameter/type names that lie about what they do) live in `playbooks/shape-vs-function.md` §6. Misleading **comments** live here. The review step deduplicates findings that span both.

## Output

Group findings by category. Per finding:

- File:line.
- Category letter + name (e.g., "A — Orphan reference").
- The comment text.
- The evidence (orphan symbol checked, lying-vs-next-stmt diff, tautology overlap, etc.).
- Confidence tier.
- For HIGH: a suggested removal diff.
- For MEDIUM: the suggested fix direction (one sentence).

End with a count summary: total comments scanned, by category, by confidence. The total-scanned figure helps calibrate the audit over runs — if total drops between runs, hygiene is improving; if it doesn't, the playbook isn't matching what's there.
