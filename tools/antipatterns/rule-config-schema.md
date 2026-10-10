# Anti-pattern detector — rule config schema

**Posted Day 1 of Sprint 2 (per slice-1 cross-coordination convention).**

This document specifies the configuration shape consumed by the anti-pattern
detector. The later spec-gate experiment was retired; it is not a consumer of
this schema or a current pre-commit/CI gate.

## Files

| File | Owner | Role |
|------|-------|------|
| `tools/antipatterns/default-config.toml` | the package | shipped defaults; edit only via PR |
| `.antipatterns.toml` (repo root) | the repo | per-repo overrides (optional) |

Per-repo overrides shallow-merge over defaults. Per-rule subsections deep-merge:
a repo can override one rule's `severity` without redeclaring all its other
fields.

## Top-level shape

```toml
[global]
exclude_globs       = [...]   # files/dirs to skip across ALL rules
test_path_globs     = [...]   # files classified as test paths (rules that
                              # only fire on production paths use this list)
config_path_globs   = [...]   # files classified as config/fixture; some
                              # rules (hardcoded-path-or-id) skip these

[rules.<rule-id>]
enabled  = true                # default true; flip to false to disable
severity = "warning"           # error | warning | info; overrides rule default
include_globs = [...]          # if non-empty, rule fires only on matching paths
exclude_globs = [...]          # rule-specific exclusions, layered on global
# rule-specific fields below (see Per-rule fields)
```

## Severity overlay precedence

1. Rule's hardcoded default (in `rules/<rule>.py`).
2. `tools/antipatterns/default-config.toml` `[rules.<rule>].severity`.
3. `.antipatterns.toml` `[rules.<rule>].severity`.

The engine canonicalizes severity per-finding to whatever the merged config
resolves to.

## Per-rule fields

### `silent-error-swallow`

```toml
[rules.silent-error-swallow]
enabled  = true
severity = "warning"
```

No additional knobs. Patterns flagged: `except: pass`, `except Exception:
pass`, debug-only logging, `return None|""|0|False|[]|{}|()`.

### `hardcoded-path-or-id`

```toml
[rules.hardcoded-path-or-id]
enabled  = true
severity = "warning"
path_patterns = [
    '^/Users/',
    '^/home/',
    '^~/\.corsia',
]
uuid_pattern = '\b[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-...'
```

`path_patterns` is a list of Python regex strings (TOML literal-string
quoted). `uuid_pattern` is a single regex. Test paths
(`global.test_path_globs`) and config paths (`global.config_path_globs`)
auto-skip.

### `fallback-chain-masks-error`

```toml
[rules.fallback-chain-masks-error]
enabled  = true
severity = "warning"
```

No additional knobs. Detects chained `.get()`/`[]` reads connected by `or`
(≥2 such reads + a terminal default), and try/except returning a non-empty
fallback value.

### `missing-public-docstring`

```toml
[rules.missing-public-docstring]
enabled  = true
severity = "warning"
include_globs = ["backend/**/*.py", "tools/**/*.py"]
```

Frontend (TypeScript) is out of scope this sprint. Test paths skip
automatically.

### `mock-where-real-needed`

```toml
[rules.mock-where-real-needed]
enabled  = true
severity = "warning"
```

Test paths auto-skip via `global.test_path_globs`.

### `untracked-todo`

```toml
[rules.untracked-todo]
enabled  = true
severity = "warning"
tracking_marker_patterns = [
    '#\d+',                        # GitHub issue ref
    '\bADR-\d+\b',                 # ADR reference
    'audit-run/session-\d',        # audit-run reference
    '\(deferred:[^)]+\)',          # explicit deferral note
    '\btask-[a-z0-9-]+\b',         # CORSIA task ID
]
```

### `periodic-server-reread`

```toml
[rules.periodic-server-reread]
enabled  = true
severity = "error"
include_globs = ["frontend/src/**/*.ts", "frontend/src/**/*.svelte"]
exclude_globs = [
    "frontend/src/lib/homeLifecycle.ts",   # debt: issue ref required per entry
]
allow_files = [
    "frontend/src/lib/components/HostAccessLeaseControl.svelte",  # reason required
]
# server_read_pattern = ''   # overrides the default helper-name regex
```

Flags a `setInterval`, or a self-rescheduling `setTimeout` (its callback
re-arms it; a poll whatever the delay, retry-with-backoff included), in
frontend `.ts`/`.svelte` files whose callback re-reads server state. The
callback is inline or a function named in the same file. It is resolved
through same-file functions (declarations and `const f = ... =>`) with a
visited set and no depth limit; a bare name that resolves is judged by its
body. A server read is `fetch(`, or a call to an identifier matching
`server_read_pattern` (default: the bare or capitalised, lowercase-leading
`load|refresh|reload|poll|fetch|sync` verbs, e.g. `refresh()`,
`store.reload()`, `loadThreads()`, plus the `read*` helpers in
`frontend/src/lib/api/`) that is unresolved: an import, or any member call
(`x.refresh()` is never resolved against same-file functions).
`*.test.ts`, `*.spec.ts`, and `__tests__/` are skipped by the rule.

- **Two lists, two meanings.** `exclude_globs` is the **debt** list: each
  entry cites a tracking issue, and it may only shrink (delete the entry when
  the poller is fixed; never add one for new code). `allow_files` is the list
  of **accepted false positives**: each entry is a reviewed per-file
  exemption with a stated reason, not debt.
- **False positives:** pure local clocks never match; a one-shot
  `setTimeout` at a known deadline (debounce, overdue time) never matches; a
  verb-named local helper with a local-only body (`refreshRelativeTimes()`)
  does not match. A deadline-driven refresh run from an interval (lease
  expiry with backoff) and a bounded retry after a failed request (a
  self-rearming `setTimeout`) cannot be told apart from a poll; their
  remedy is an `allow_files` entry with the reason.
- **Residual known class:** a member call is always name-tested, so a
  verb-named method with a local-only body (class method, object-literal
  method, `x.refresh()` while the file also defines a local `refresh`) is
  flagged. Remedy: rename the helper, or an `allow_files` entry with a
  reason.
- **Fix:** emit a websocket event from the backend producer and refresh from
  its handler and on reconnect; for a time-driven change, arm one
  `setTimeout` at the deadline.

Each TOML string is passed unmodified to Python's `re.compile()` as a regex.
TOML *literal* strings (single quotes) avoid backslash escape processing —
`'\d'` in TOML is the two-character regex `\d`.

## CLI surface

```
python -m antipatterns --staged                    # scan git-staged files
python -m antipatterns --paths f1 f2 f3            # explicit list
python -m antipatterns --all                       # whole repo (calibration)
python -m antipatterns ... --severity error        # block-on-error (default)
python -m antipatterns ... --severity warning      # block-on-warning
python -m antipatterns ... --format json           # machine-readable
python -m antipatterns ... --format human          # terminal (default)
python -m antipatterns ... --rule <id> [--rule <id>]
                                                   # restrict to listed rules
python -m antipatterns ... --repo-root /path       # override cwd
```

Exit codes: 0 = clean (no findings at or above threshold); 1 = blocking
findings present; 2 = invocation error.

## Finding JSON schema

```json
{
  "findings": [
    {
      "rule_id":   "silent-error-swallow",
      "severity":  "warning",
      "file":      "backend/foo.py",
      "line":      42,
      "col":       9,
      "snippet":   "    except Exception:",
      "message":   "...",
      "fix_hint":  "...",
      "extra":     {}
    }
  ]
}
```

Findings are sorted by (file, line, col, rule_id) for deterministic diffs.
`col` may be `null` for line-level findings; `fix_hint` may be `null` if
the rule didn't ship one.

## Pre-commit hook entry

```yaml
# .pre-commit-config.yaml
- repo: local
  hooks:
    - id: antipatterns
      name: antipatterns (proactive lint-class checks)
      entry: python -m antipatterns --staged --severity error
      language: system
      pass_filenames: false
      types_or: [python, ts, svelte]
```

The hook command runs `--severity error` so only errors block. Warnings
still surface in the terminal output, but the gate doesn't block.

## Retired experiment note

The historical spec-gate hook and subsystem-boundary registry are retired.
Current gate behavior is defined by the hooks and workflows that actually
exist in the repository.
