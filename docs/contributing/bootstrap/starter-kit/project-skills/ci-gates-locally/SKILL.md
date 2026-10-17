---
name: ci-gates-locally
description: Use before pushing a branch, when a CI check fails or never reports, or when a merge gate blocks—run every blocking gate on this machine first, separate a CI infrastructure outage from a real failure, and never publish the upstream reviewer status from a contributor workspace.
---

# Running the CI gates locally

Every code gate that can block a merge can run on this machine; `bash scripts/check-pr` runs the local set. Run them before pushing, so a push confirms a known result instead of discovering one. Use `corsia-test-environment` to select the interpreter and isolate state; this skill covers which gates exist, how to run each, and how to read a failure.

Install the pre-push gate with `git config core.hooksPath .githooks`.

## Run before pushing, not after

Push is confirmation, not discovery. A branch that has not passed every blocking gate locally is not ready to push. Upstream CI runs on GitHub-hosted runners whose minutes the maintainers pay for; every avoidable red run costs them money and attention.

## The gates

| Gate | Blocking | Command |
|---|---|---|
| Backend unit tests | yes | CI-owned full suite; exact pytest flags live in `.github/workflows/unit-tests.yml` |
| Anti-pattern detector | yes | `PYTHONPATH=tools python -m antipatterns --all --severity error --repo-root .` |
| mypy error limits | yes | `mypy backend/ > b.txt; mypy tools/ > t.txt; python scripts/mypy_error_limits.py backend=b.txt tools=t.txt` |
| `qa/llm-reviewer` | yes — merge gate, **maintainer-owned** | Never publish it from this workspace. Run the self-review below instead. |
| Frontend unit tests | yes | from `frontend/`: `npm run test` |
| ruff | advisory | `ruff check .` |

Read `.github/workflows/*.yml` for the exact invocation rather than reconstructing it. Match the flags: a local run that omits a flag CI passes proves less than it appears to.

The backend full suite is CI-owned: one result per exact candidate head.
Worktree agents run only the smallest affected tests and never invoke the full
suite locally. Record the head SHA with the CI result so another actor can
reuse it. Reproduce failures with targeted tests before pushing a new head.
Do not add pytest workers or parallel loops locally.

The anti-pattern gate blocks on `error` severity only. A large warning count is normal and does not fail the build; do not read `0 error(s), N warning(s)` as a failure.

mypy tolerates a committed backlog through per-target limits in `quality/mypy-error-limits.json`. It fails only when a target reports more errors than its limit. When a count drops, lower the limit in the same change; the check prints the replacement value.

## The reviewer status belongs to the maintainers

`qa/llm-reviewer` is the upstream merge gate. It is not a hosted action: the maintainers' CORSIA instance runs it (`backend/reviewer/`) after non-reviewer checks complete successfully for a ready PR, or when a draft becomes ready if those checks are already green, and publishes a commit status. Draft PRs still run CI but do not trigger the LLM reviewer. From a contributor workspace:

- Never publish, re-post, or override `qa/llm-reviewer` (or any `qa/*` status or label) on the upstream repository, even if your instance could.
- Before pushing, run your own review pass on the diff with a fresh-context reviewer subagent against `sdlc-process` and the development doctrine, and link its result under **First-pass evidence**.
- A missing or stale upstream review is the maintainers' infrastructure; comment once on the PR with the head SHA and wait.

## Separating an outage from a failure

A red check is not evidence of a code failure until the failing step is identified.

- `Failed to resolve action download info` or `Service Unavailable` at `Set up job` is an Actions outage. No repository code ran.
- A `cancelled` conclusion with an empty `steps` array is infrastructure, not a test result.
- Confirm with the Actions component at `https://www.githubstatus.com/api/v2/components.json`, and report suspected runner trouble on the PR; runner health is maintainer-owned.

When Actions is degraded, new pushes may not trigger runs at all. Verify by comparing the PR head SHA against the most recent run rather than waiting.

## Reading a hang

`faulthandler_timeout` dumps stacks after a stall but does not stop the run; the process keeps hanging and the dump may contain only framework frames.

- Check `%CPU` before concluding anything. Zero CPU with progress still advancing is ordinary SQLite I/O waiting. Zero CPU with no progress for minutes is a hang.
- Test order is deterministic: `pytest-randomly` is not installed, and `-p no:randomly` is a no-op. Verify rather than assume — compare `pytest --collect-only -q` output across two runs.
- Because order is fixed, a hang reproduces at the same point. Locate it with `-v` so the last name printed is the hanging test.
- A file that passes alone but hangs in the full run is an isolation leak from an earlier test, not a defect in that file.
- To attribute a hang, run the same suite in a clean worktree at the merge base. Identical behaviour there means it is pre-existing.

## Measure before explaining

Every wrong diagnosis in this area has come from explaining before measuring — attributing slowness to the wrong machine, reading an averaged `%CPU` as a hang, blaming randomized ordering that was never enabled. State the measurement, then the conclusion. If the measurement has not been taken, say so instead of offering a cause.

## Pushing workflow changes

An OAuth token without `workflow` scope cannot modify `.github/workflows/`. Pushing over SSH is not subject to OAuth scopes and succeeds where the HTTPS remote is rejected. Do not weaken the change or split it out solely to work around the scope.
