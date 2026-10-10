# Codebase-Health Harness — Methodology

A repeatable, apples-to-apples quality assessment. Run it once before a
tech-debt initiative (the **BASELINE**) and again after (the **AFTER**); the
two reports are directly comparable because every instrument, version, weight,
and grade boundary below is pinned. Changing any pinned value invalidates the
comparison — if a model change is unavoidable, **re-run both refs**.

## Scope & roots

| Stack | Source roots | Test roots (excluded from "source" metrics) |
|---|---|---|
| Backend (Python/FastAPI) | `backend/`, `tools/` | `**/tests/`, `test_*.py`, `*_test.py`, `conftest*` |
| Frontend (SvelteKit/Svelte5+TS) | `frontend/src/**/*.{svelte,ts}` (excl `*.d.ts`) | `*.test.ts`, `*.spec.ts`, `**/__tests__/`, `frontend/e2e/` |

## Isolation rules

- Analysis tools install into a **dedicated venv** (`/tmp/health-venv-<ref>`),
  **never** the repo venv. Rationale: keep runtime deps uncontaminated.
- Coverage instruments need the repo's runtime deps (litellm, fastapi, …). They
  run through the **repo interpreter** (`.venv`, Python 3.14) with
  `coverage`/`pytest-cov` made importable via `PYTHONPATH` to the analysis venv
  site-packages — so the repo venv is still not modified.
- `pip-audit` runs in a **Python-3.11** analysis venv (`/tmp/health-venv311`)
  because the litellm pin requires `<3.14`.
- Assessment runs against a **pinned worktree** at the target ref
  (`git worktree add .worktrees/health-<ref> <ref>`) so later merges can't
  contaminate the "before."

## Pinned tool versions

| Instrument | Version | Interpreter |
|---|---|---|
| radon | 6.0.1 | py3.14 analysis venv |
| ruff | 0.15.17 | py3.14 |
| mypy | 2.1.0 | py3.14 |
| bandit | 1.9.4 | py3.14 |
| vulture | 2.16 | py3.14 |
| grimp | 3.14 + networkx 3.6.1 | py3.14 |
| coverage / pytest-cov | 7.14.1 / pytest 9.0.3 | repo `.venv` (py3.14) |
| pip-audit | 2.10.1 | py3.11 analysis venv |
| jscpd | 4.2.5 | node v25.9.0 (`npx`) |
| svelte-check | 4.6.0 | worktree `node_modules` |
| vitest + @vitest/coverage-v8 | 4.0.18 | worktree `node_modules` |
| tsc (typescript) | 5.9.3 | worktree `node_modules` |
| antipatterns (repo's own) | repo `tools/antipatterns` @ ref | py3.14 analysis venv |

## The exact commands (reproducible)

> Run from the pinned worktree root unless noted. `$V` = analysis venv,
> `$V311` = py3.11 venv, `$REPO_VENV` = repo `.venv`.

### 1. Complexity / maintainability
```
$V/bin/radon cc backend tools -j > cc.json     # cyclomatic complexity
$V/bin/radon mi backend tools -j > mi.json      # maintainability index
```
Metrics: `% functions CC>15`, max CC, worst-20 list, MI mean.

### 2. Size budgets (repo standards: Py/Svelte≤400, TS≤300, funcs≤80)
```
python tools/codebase-health/metrics.py --worktree . --out <dir> --venv $V
```
(AST-based file-LOC + per-function-length counting; see `metrics.py`.)

### 3. Duplication
```
npx jscpd@4 backend tools --pattern "**/*.py" --ignore "**/__pycache__/**" --reporters json --output <dir>/jscpd-py
npx jscpd@4 frontend/src --pattern "**/*.{ts,svelte}" --ignore "**/node_modules/**,**/*.d.ts" --reporters json --output <dir>/jscpd-fe
```
Metric: weighted duplication % (by lines) + clone count.

### 4. Test coverage
```
# Backend (run from backend/, repo interpreter, isolated workspace):
CORSIA_WORKSPACE=/tmp/corsia-health-ws CORSIA_ANALYTICS_DISABLE=1 \
PYTHONPATH="$V/lib/python3.14/site-packages:$PYTHONPATH" \
$REPO_VENV/bin/python -m pytest -m "not cloud_required and not integration" \
  --cov=. --cov-branch --cov-report=json:cov.json -q -p no:cacheprovider

# Frontend (from frontend/, needs `npm ci` first):
npx vitest run --coverage
```
Metrics: backend line + branch %, frontend statement %, test-file/source-file counts.

### 5. Type safety
```
$V/bin/mypy backend --ignore-missing-imports --exclude '(/__pycache__/|/\.venv|/tests/)' --no-error-summary
(cd frontend && npm run check)   # svelte-check 4.6.0 (svelte-kit sync + svelte-check)
```
NOTE: the repo ships **no mypy config and no ruff config** — both run in
**default mode** (mypy: untyped bodies not checked; ignore-missing-imports on).
State this in every report. The numbers measure latent type debt, not gate failures.

### 6. Lint
```
$V/bin/ruff check backend tools --output-format=json
```
ESLint is **not configured** in this repo — reported as N/A, not zero.

### 7. Security
```
$V/bin/bandit -r backend tools -f json -q          # bucket by severity; exclude B101 asserts in tests
$V311/bin/pip-audit -r backend/requirements.txt --format json --skip-editable
(cd frontend && npm audit --omit=dev --json)        # prod deps only
```
Bandit headline = **MED/HIGH in source** (B101 `assert_used` in tests is excluded as noise).

### 8. Dead code
```
$V/bin/vulture backend tools --min-confidence 80
```

### 9. Dependency health
```
$V311/bin/pip-audit -r backend/requirements.txt ...   # vuln count
(cd frontend && npm audit --omit=dev --json)           # vulnerable prod pkgs by severity
```

### 10. Architecture coherence
```
# Import cycles (grimp + networkx SCC):
$V/bin/python -c "import grimp,networkx as nx; g=grimp.build_graph('backend'); ..."
# Repo's own antipattern suite, warning+error, bucketed by rule:
PYTHONPATH=tools $V/bin/python -m antipatterns --all --severity warning --repo-root .
```
Plus: known forked/duplicate paths
(cross-check the dated tech-debt review).

### 11. Local-LLM reasoning suitability (D11) — added 2026-06-12

Implements the engineering-standards lens: "easy for humans AND local LLMs to reason
about." All four sub-metrics are collected inside `metrics.py:llm_suitability()` and
require only the analysis venv (no extra tools). Sub-metrics and commands:

**a. Token-budget fit**
```
# Estimator: tokens = len(file_text) / 4  (CHARS_PER_TOKEN constant in metrics.py)
# This is the cl100k_base / tiktoken heuristic for mixed prose+code; stated explicitly
# so it is identical across runs. No tokeniser binary is required.
# Headline: % of source files (.py + .svelte + .ts) whose estimated token count ≤ 8000.
# Also recorded: % ≤ 2000 and % ≤ 4000.
```
Rationale: a local 9B doing routine maintenance typically has an 8k–16k context window.
Files under 8k tokens can be read whole; larger files force chunking.

**b. Function digestibility**
```
# AST-based count over non-test Python source files.
# A function is "digestible" iff: LOC ≤ 80 AND max compound-statement nesting depth ≤ 4.
# (Reuses size_budgets data; nesting computed via recursive AST walk in _max_nesting().)
# Metric: % functions meeting both criteria.
```
Nesting depth counts: `for`, `while`, `if`, `with`, `try`, `except` blocks.

**c. Context coupling**
```
# Import fan-in and fan-out per module (grimp.build_graph('backend')):
import grimp, statistics as S, json
g = grimp.build_graph("backend")
mods = list(g.modules)
fi = [len(g.find_modules_that_directly_import(m)) for m in mods]
fo = [len(g.find_modules_directly_imported_by(m)) for m in mods]
# Metrics: median and p95 of fan-in; median and p95 of fan-out.
# "files you must hold in context to understand one" ≈ p95(fan-in) + p95(fan-out).
```

**d. Docstring coverage**
```
# AST-based count: for each public (non-underscore) function in non-test Python source,
# check whether body[0] is a string-literal Expr node (a docstring).
# Metric: % public functions with docstrings.
# Standard (from engineering-standards.md): "All public functions should have docstrings."
```

**Composite (D11)**
The four sub-metrics are averaged with equal weight:
```
llm_score = (token_fit_pct + func_digestibility_pct + coupling_score + docstring_pct) / 4
```
where `coupling_score = max(0, 100 − (p95_fanin + p95_fanout) × 5)`.
The same `pct_to_grade()` thresholds apply (A≥90, B≥80, C≥70, D≥60, else F).
Remediation cost is not modelled per-finding for D11 (improvements are structural,
not countable as individual findings); the dimension contributes to the composite
but adds 0 to the SQALE debt total.

### LOC ledger (Addition 2) — added 2026-06-12

Collected by `metrics.py:loc_ledger()` using **cloc 2.08**.
Prerequisite: `cloc` on PATH (brew install cloc / apt install cloc).
If absent, the ledger keys are omitted from metrics-partial.json (not a fatal error).

**Excluded from all counts:** `node_modules`, `.venv*`, `dist`, `.svelte-kit`,
`coverage`, `.worktrees`, lockfiles (`*.lock`, `package-lock.json`).

**Buckets and exact commands:**
```
# Backend source (Python, excl test files):
cloc backend/ tools/ --include-lang=Python --exclude-dir=... --csv --quiet

# Backend tests only (test_*.py / *_test.py / conftest*):
cloc backend/ tools/ --include-lang=Python --match-f='(test_|_test\.py$|conftest)' --exclude-dir=... --csv --quiet

# Frontend (Svelte + TypeScript, excl *.d.ts):
cloc frontend/src/ --include-lang=Svelte,TypeScript --not-match-f='\.d\.ts$' --exclude-dir=... --csv --quiet

# Specs (specs/ Markdown):
cloc specs/ --include-lang=Markdown --exclude-dir=... --csv --quiet

# Docs (all other .md, excluding specs/):
cloc . --include-lang=Markdown --exclude-dir=...,specs --csv --quiet
```

Output keys in metrics JSON: `loc_{bucket}_{files,code,comment,blank}` for each bucket.

## Scoring model (SQALE-style)

### Per-dimension
Each dimension gets a **0–100 goodness sub-score** (higher = healthier) via a
fixed transform in `score.py`, then a letter grade on an **academic best-practice
scale**: A≥90, B≥80, C≥70, D≥60, else F. (Coverage uses a looser 85/70/55/40.)
This index is deliberately strict: "F" means "well below best practice on this
axis," **not** "broken."

### Composite (0–100)
Weighted sum of the sub-scores. Weights (sum=1.0), stated so the after-run is identical:

| Dimension | Weight | | Dimension | Weight |
|---|---|---|---|---|
| coverage | 0.1615 | | security | 0.1140 |
| complexity | 0.1425 | | type_safety | 0.1140 |
| size_budgets | 0.0950 | | duplication | 0.0760 |
| lint | 0.0760 | | architecture | 0.0760 |
| dead_code | 0.0475 | | dependencies | 0.0475 |
| **llm_suitability** | **0.0500** | | | |

**Re-weighting note (2026-06-12):** D11 `llm_suitability` was added at weight 0.05.
All prior 10-dimension weights were scaled by ×0.95 to keep the sum at 1.0.
Original 10-dim weights: coverage 0.17, complexity 0.15, type_safety 0.12,
security 0.12, size_budgets 0.10, duplication 0.08, lint 0.08, architecture 0.08,
dead_code 0.05, dependencies 0.05.

### Technical-debt ratio (the SQALE headline)
```
remediation_cost = Σ (finding_count × per_finding_minutes)   # table in score.py
development_cost  = total_source_LOC × 30 min/LOC             # SonarQube/SQALE default
debt_ratio        = remediation_cost / development_cost
```
Per-finding remediation minutes (modelled on SonarQube defaults): CC>15=30,
oversized-file=60, over-long-func=20, dup-clone=15, uncovered-stmt=1,
mypy-error=10, svelte-error=10, ruff=5, bandit-MED/HIGH=30, vulture=10,
npm-high=60, npm-mod=30, antipattern-warn=10, antipattern-err=30, cycle=120.

**Debt-ratio grade boundaries (SQALE rating):** A≤5%, B≤10%, C≤20%, D≤50%, E>50%.

### Two headline numbers, on purpose
- **Debt ratio** answers "how much is the debt vs. the cost to rebuild?" — the
  industry-standard SQALE rating. Real codebases almost always land A here.
- **Composite (0–100)** answers "how close to best practice on each axis?" — a
  stricter health index that surfaces *which* axes lag even when total debt is small.

Report both. Do not let one mask the other.

## Reproducing end-to-end
```
tools/codebase-health/run.sh <ref> <out-dir>     # automated instruments
# then run §4 coverage + §5 svelte-check + §7 npm-audit manually (need repo/node deps),
# merge their numbers into <out-dir>/metrics.json, then:
$V/bin/python tools/codebase-health/score.py <out-dir>/metrics.json
```
