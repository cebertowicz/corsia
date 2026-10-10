# Playbook: code-health (LLM-readability metrics)

Find files, functions, and identifiers whose **size or shape** makes the codebase hard to reason about — for humans, and disproportionately for smaller local LLMs (Qwen 3.6 / 35B-class) whose effective working memory is tighter than frontier models.

This is the most quantitative track. The thresholds below are bound to `~/.claude/CLAUDE.md` → `engineering-standards.md` and are project-configurable; if a project sets its own limits in `CLAUDE.md`, prefer those.

## Why this matters more for LLMs

A human can scroll. A local LLM running with a 32K context cannot — a 600 LOC TypeScript file consumes ~12K tokens, leaving ~20K for everything else (other files, tool results, the response). The same file in a 1M-context frontier model is invisible.

The shape-vs-function track (`playbooks/shape-vs-function.md`) finds code that *misleads* an LLM. This track finds code that *exhausts* it.

## Default thresholds (override per-project via CLAUDE.md)

| Metric | Threshold | Rationale |
|---|---|---|
| File LOC — TypeScript / TSX | 300 | engineering-standards.md |
| File LOC — Python | 400 | engineering-standards.md |
| File LOC — Svelte | 400 | engineering-standards.md |
| File LOC — other | 500 | sane default |
| Function LOC | 80 | engineering-standards.md |
| Nesting depth | 4 | engineering-standards.md |
| Cyclomatic complexity (per fn) | 15 | engineering-standards.md target |
| Public-function docstring | required | engineering-standards.md |
| Identifier minimum length | 3 | exceptions: `i`, `j`, `id`, `e` (catch), `_` |
| Single-file token budget | 24 000 tokens (≈ 80 KB) | local-LLM context guard |

If `CLAUDE.md` in the target repo sets different numbers, use those.

## What to look for

### 1. Oversized files

Files exceeding the per-language LOC threshold.

**Detection:** `wc -l` per file, filter by extension, threshold per language.

**Confidence tier:**
- `HIGH` (severity, not action) if file is ≥ 2× threshold (e.g., 600+ LOC TS file).
- `MEDIUM` if 1.0–2.0× threshold.
- `LOW` if 0.8–1.0× (approaching).

**Action:** report only. Splitting a file is a design call. Suggest possible split axes: by export, by class, by feature.

### 2. Oversized functions

Functions whose body exceeds the function LOC threshold (80 by default).

**Detection (Python):** AST `FunctionDef`/`AsyncFunctionDef` body span; count lines from `def` to last statement.
**Detection (TypeScript / JavaScript):** AST-aware preferred (`tsc --noEmit` + a tree-sitter parser if available); fallback heuristic — count lines between matching braces of a `function`/method/arrow-with-block.
**Detection (Svelte):** treat the `<script>` block as a TS/JS module and apply the TS rules.

**Confidence tier:**
- `HIGH` (severity) if function body ≥ 2× threshold (160+ LOC).
- `MEDIUM` if 1.0–2.0×.
- `LOW` if 0.8–1.0×.

**Action:** report only. Suggest possible extraction points (loops, large conditionals, multi-step transforms).

### 3. Excessive nesting

Functions whose maximum nesting depth exceeds the threshold (4).

**Detection:** AST-based depth count; nodes that increase depth: `if`, `for`, `while`, `switch`/`match`, `try`/`except`, `with`, function-in-function.
**Heuristic fallback:** count leading whitespace runs; depth ≈ max indent / indent-unit.

**Confidence tier:**
- `MEDIUM` if depth > threshold.
- `LOW` if depth = threshold (right at the line).

**Action:** report. Common cures — early returns / guard clauses, extracting inner blocks to functions, replacing nested conditionals with table dispatch. Don't auto-refactor.

### 4. High cyclomatic complexity

Functions with > 15 decision points.

**Detection:** count `if`/`elif`/`else if`, `for`, `while`, `case`/`when`, `&&`, `||`, `?:` ternaries, `except`/`catch` clauses inside the function body. Add 1 for the function's own entry. (This is a heuristic — for a precise count use a static analyzer like `radon` for Python, `eslint-plugin-complexity` for TS.)

**Confidence tier:**
- `HIGH` (severity) if complexity > 30.
- `MEDIUM` if 16–30.
- `LOW` if 13–15 (approaching).

**Action:** report. Often a complexity offender is also an oversized function — flag once, with both metrics.

### 5. Missing docstrings on public functions

Public functions (not prefixed with `_`, not file-local in TS, exported) without any docstring/JSDoc.

**Detection (Python):** any `def f(...)` not starting with `_`, where the function body's first statement is not a string literal.
**Detection (TypeScript):** any `export function`, `export const f =`, exported method, or class member with `public` (or no modifier) lacking a preceding JSDoc comment.

**Confidence tier:**
- `MEDIUM` for any missing docstring on public function.
- Promote to `HIGH` *severity* for public functions with non-trivial body (>10 LOC) or that perform I/O / mutation.

**Action:** report only. Auto-generating docstrings risks AI-style verbose noise (which the slop track will then have to remove). Surface the list; let the human author the docstrings.

### 6. Identifier hygiene

Cryptic or sub-3-character identifiers in non-trivial scopes.

**Detection:**
- Names ≤ 2 chars in any scope wider than a 5-line block (allow `i`, `j`, `id`, `_`, exception variable `e`).
- Common low-value abbreviations as identifiers: `mgr`, `ctx`, `cfg`, `tmp`, `obj`, `arr`, `val`, `res`, `req` — flag in non-trivial scopes (where context doesn't make them obvious).

**Confidence tier:**
- `MEDIUM` for identifiers that genuinely could be more descriptive.
- `LOW` for borderline cases — only flag when 3+ in the same file (pattern, not individual lapses).

**Action:** report. Renaming is a design choice; don't auto-apply.

### 7. Single-file token-budget violation

File size in estimated tokens > 24 000 (≈ 80 KB of source).

This isn't redundant with file-LOC: a 100 LOC file that's mostly a giant string-literal table can blow the budget where a 600 LOC file of normal code does not.

**Detection:** estimate tokens — heuristic ≈ `bytes / 4`; or use `tiktoken` if available. Flag any source file that exceeds 24K tokens.

**Confidence tier:**
- `MEDIUM`. Always REPORT — fix is project-specific (split, externalize the data, etc.).

**Action:** report. Suggest moving large literal data to JSON / TOML / .data.ts files when applicable.

### 8. Hardcoded IDs / paths (engineering-standards.md violation)

Literal IDs or absolute paths in code outside config files.

**Detection:** UUID-shaped strings, hostnames, full filesystem paths (e.g., `/Users/...`, `/var/...`, `C:\...`), database IDs (numeric literals passed as IDs).

**Confidence tier:**
- `MEDIUM` for paths/IDs that look load-bearing.
- `LOW` for one-off literals in tests / fixtures.

**Action:** report. Suggest moving to a config / env-var / fixture file.

## Evidence to collect

Per finding:
- File (and line / function name where applicable).
- Metric, observed value, threshold.
- For oversized files/functions: a sketch of what occupies the bulk (longest sub-block, top-3 sub-functions by size).
- For complexity: the count of decision points, broken down.
- For docstrings: the function signature and a 1-line description of what it does (so the human has a starting point).
- For identifier hygiene: the 3+ examples in one file, with surrounding context.

## Rules

- **This track REPORTS; it does not implement.** Code-health refactors are design calls. Even moving a 200-LOC function into two halves can change semantics in subtle ways (closure capture, exception scope, test coverage boundaries).
- **Don't double-flag.** If a function is both oversized and high-complexity, surface one finding with both metrics — not two.
- **Honor project overrides.** If the target repo's `CLAUDE.md` sets thresholds, those win. Note in the report which thresholds were used and where they came from.
- **Be specific in suggestions.** "Consider splitting" without naming a split axis is noise. Either name a candidate axis or omit the suggestion.

## What NOT to flag

- Test files (separate budgets — long, repetitive test setup is OK).
- Auto-generated files (`*.gen.ts`, `*_pb2.py`, `schema.graphql.ts`, prisma client output). Detect by header marker or by path conventions.
- Vendored / third-party code in `vendor/`, `node_modules/`, `.venv/`.
- Migration files (often legitimately long single statements).
- Schema / type-definition files where the LOC reflects the type system, not the logic.
- Files that are deliberately a single large config blob (e.g., `eslint.config.ts`, `routes.ts` if it's a registry).

## Output

Per finding cluster (one per file is usually right):

```
### [TIER] <file path>
- File: <path>  (<lang>, <X> LOC)
- Issues:
  - File LOC: <N> / <threshold> (factor: <ratio>×)
  - Functions over budget: <name1> (<N> LOC), <name2> (<N> LOC)
  - Max nesting: <depth> (threshold <T>)
  - High-complexity functions: <name1> (cc=<N>), ...
  - Missing docstrings: <count>
  - Token estimate: <N> (threshold <T>)
- Suggested split axes (if applicable): <one-line direction>
```

End with a per-language and per-metric summary table so progress can be tracked across runs:

```
| Lang | Files scanned | Files over | Funcs over | Avg cc | Coverage of public docstrings |
|------|---------------|------------|------------|--------|------------------------------|
| TS   | 142           | 11         | 7          | 6.1    | 78%                          |
| PY   | ...           | ...        | ...        | ...    | ...                          |
```

Calibration: the metrics block at the bottom is the calibration signal. Across runs of the same repo, the "files over" and "funcs over" numbers should trend down. If they don't, either the team isn't acting on findings or the thresholds need tightening.
