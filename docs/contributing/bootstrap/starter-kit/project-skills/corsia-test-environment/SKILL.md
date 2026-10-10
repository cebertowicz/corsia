---
name: corsia-test-environment
description: Use before any claim, inspection, status check, restart, deploy, or browser visit involving a running CORSIA instance (your serving install or a worktree test instance), and before installing dependencies or running any CORSIA backend, frontend, integration, QA, browser, E2E, reviewer, or full-suite test—resolve which runtime you are touching, select the supported Python/Node environment, isolate workspace/state/cache/ports, use the correct entry point, and distinguish source failures from environment or host-harness failures.
corsia:
  handler:
    kind: instruction
---

# CORSIA test environment

Use one explicit environment per task. Old plans and ad hoc commands are historical evidence; current CI, dependency locks, test fixtures, and supported scripts define the contract.

## Preflight

1. Run only in the task's linked feature worktree, never a serving checkout or another owner's worktree.
2. Record the exact worktree, test tier, interpreter, and dependency source in the owning structured plan node before the first test.
3. Inspect `git status` before setup. Preserve unrelated files and never create a root `uv.lock`.
4. Do not use bare `python`, `pip`, or `pytest`, and do not infer an adjacent virtualenv.

## Runtime identity and lifecycle

Keep these two runtimes distinct before any status, lifecycle, browser, or completion claim:

- **Serving install:** the CORSIA instance this workspace runs on (by default port 1956, from the checkout the installer created). Global `corsia ...` lifecycle commands address it. It is a deployment surface, never a development worktree: verify the exact checkout, branch, commit, API version, and listener before changing or describing it; a port alone is not proof.
- **Worktree test instance:** run from the task's linked feature worktree on a port other than the serving install's. Use CORSIA managed background processes with an explicit duration long enough for the browser pass; do not use global lifecycle commands, launchd, `nohup`, or host access merely to keep it alive. Give every run fresh task-owned workspace, state, cache, and logs. If those roots live under the user's home directory, set `CORSIA_ALLOW_HOME_WORKSPACE_IN_CI=1` only after verifying they are disposable and distinct from the serving install's. The checkout-bound `scripts/corsia` refuses mutating lifecycle commands from a non-serving checkout unless `CORSIA_WORKSPACE`, `CORSIA_STATE_DIR`, and a non-default `BACKEND_PORT` are all set.

The worktree runtime is disposable test evidence. Never promote a worktree pass into a claim about the serving install.

The GitHub notifications poller is off under `CORSIA_TEST_MODE=1`; to run it live on a disposable instance, also set `CORSIA_GITHUB_NOTIFICATIONS_TEST_ENABLE=1` (inert outside test mode, and `CORSIA_GITHUB_NOTIFICATIONS_DISABLE=1` still wins).

## Backend contract

- Use Python 3.11. Verify the selected executable reports `3.11`; reject 3.12, 3.13, 3.14, or an unknown shim for normal backend evidence.
- Prefer an existing worktree `.venv` only when its interpreter is 3.11 and required imports succeed. Otherwise create it with `python3.11 -m venv .venv`.
- Install with the selected interpreter and the pinned `backend/requirements.lock`. It already contains pytest, pytest-asyncio, and Pillow. Use a task-owned temporary pip cache when the sandbox cannot use the host cache.
- Do not introduce `uv`, a second dependency graph, or an untracked lockfile merely to run tests.

For every backend run, use fresh disposable `CORSIA_WORKSPACE` and `CORSIA_STATE_DIR`, plus `CORSIA_TEST_MODE=1` and `CORSIA_ANALYTICS_DISABLE=1`. Never point either variable at production or reuse one run's state for another full suite. `backend/conftest.py` creates ephemeral roots only when ambient overrides are absent; explicit fresh roots prevent an inherited environment from defeating that protection.

Run pytest through the selected interpreter with `PYTHONPATH=backend`. Start with the smallest affected test set. The normal local unit tier excludes `integration` and `cloud_required`; broaden only when DOD requires it.

### Local test scope

- Run only the smallest targeted tests that cover the changed behavior and its direct dependencies. Do not expand a local run to `backend/tests` for confidence, after a targeted failure, or because another agent requested broad verification.
- Never run the backend full suite from a worktree. CI owns one full-suite result for the exact PR head. Record that result with the head SHA and reuse it.
- A full-suite failure does not authorize a local full-suite rerun. Reproduce and diagnose it with the smallest targeted test set, fix it, and push one new candidate head for CI.
- Do not add `pytest-xdist`, parallel loops, or independent runner processes to worktree runs. The upstream CI owns sharding and runner capacity; do not copy its configuration into agent commands.

## Frontend contract

- Require Node 20 or newer.
- From `frontend/`, use `npm ci`, never an ad hoc install, so `package-lock.json` owns dependency resolution.
- Run focused Vitest first, then `npm run test` and `npm run build` when the affected surface warrants them. Report `npm run check` separately if repository-wide pre-existing errors remain.

## Browser and E2E contract

- For normal agent-driven CORSIA browser checks, use the native `browser_open`, `browser_snapshot`, `browser_click`, `browser_fill`, `browser_screenshot`, `browser_eval`, `browser_wait`, and `browser_close` tools. They are bounded, host-integrated wrappers around the installed agent-browser CLI, not a competing browser engine. Use the CLI directly only when an advanced agent-browser capability is required that the native surface does not expose; do not install or launch a second browser stack by default.
- Use `CORSIA_E2E_MOCK=1 bash scripts/e2e-local` for deterministic local E2E. Pass `CORSIA_E2E_PYTHON` when the validated interpreter is not at a supported `.venv` path.
- Do not invoke Playwright directly when `scripts/e2e-local` owns backend seeding, workspace/state isolation, and cleanup.
- Run one E2E owner at a time. Before starting, verify ports 3110 and 5178 are not owned by another live task; the current script uses fixed ports and may terminate occupants. Use `--reuse` only for servers owned by the same task.
- When DOD requires a manual real-provider smoke, start the worktree backend and frontend as separate managed processes on task-owned ports, initialize the isolated schema, seed only the isolated database with `scripts/seed-worktree-db`, and supply an explicit reachable provider/model plus `CORSIA_E2E_MODEL`. Use only the synthetic fixture credential (`TEST_TOKEN` in `frontend/e2e/support/corsia.ts`) to sign in. Verify the rendered response and model attribution, then close the browser, stop both managed processes, and prove both ports closed.
- Real-provider mode is authorized only when DOD requires it. A provider response does not excuse a later delivery, checkpoint, or rendering failure; report each layer separately.

## Failure classification

- **Source failure:** the test reaches the intended environment and produces an assertion, type, build, or behavior failure attributable to the candidate.
- **Environment failure:** wrong interpreter, missing dependency/import, stale/reused state, accidental production path, occupied port, or lock/cache mismatch. Repair the environment; do not patch product code.
- **Host-harness failure:** Safehouse/profile expiry, launchd assumptions, browser crash, file-descriptor exhaustion, or a test requiring unavailable host integration. Record it as infrastructure evidence and use the supported CI/host runner; do not use retry loops merely to manufacture green.

A local pass does not replace current-head CI. If the supported environment cannot be established, stop and report the exact missing prerequisite rather than substituting another Python, dependency source, state root, or test tier.
