# Testing and review

Run focused tests and the local gate before pushing, then use the pull request checks and review to finish your change.

## Before you push

Run the smallest tests that cover the changed behavior. For backend tests, use the Python 3.11 `.venv` from [Developer setup](dev-setup.md) and set both paths to fresh disposable directories, so the tests never touch a CORSIA installation you use, before running pytest from `backend/`:

```bash
export CORSIA_WORKSPACE="$(mktemp -d)"
export CORSIA_STATE_DIR="$(mktemp -d)"
export CORSIA_TEST_MODE=1 CORSIA_ANALYTICS_DISABLE=1
cd backend
../.venv/bin/python -m pytest tests/path/to/test_file.py
```

For frontend tests, run `npm ci` in `frontend/`, then `npm run test -- path/to/test` for a focused test or `npm run test` when the full frontend suite is appropriate. Use `npm run check` for the frontend type check. See [`frontend/e2e/README.md`](../../frontend/e2e/README.md) for browser tests.

From the repository root, run `bash scripts/check-pr` before pushing. It checks workflow syntax when actionlint is installed, file-size and design-token limits, anti-patterns, mypy limits, tools tests, documentation drift and read cost when relevant. It does not replace targeted tests. `--fast` is an alias for the default; `--full-backend` is CI-only. CI owns the full backend suite for each PR head, so do not run it locally.

## After you push

GitHub Actions uses GitHub-hosted runners. Depending on the changed paths and target branch, jobs include **Backend unit tests**, **Backend unit tests (macOS-only files)**, **Frontend unit tests**, **Integration tests (L3)**, **File size ratchet**, **mypy (regression gate)**, **ruff (advisory)**, **Anti-pattern detector (error-severity, blocking)**, **Documentation drift check (citations + private-name scan)**, **No file over 500 KB**, **Read-cost speed gate**, **Native browser lifecycle**, **Live host reviewer proof (Haiku)** and **Cache evals (local oMLX, advisory)**. Some jobs are skipped when irrelevant; some workflows run only for particular branches or paths. The macOS unit job runs files listed in `backend/tests/macos_only_tests.txt`; the Linux job excludes them. PRs to `main` with relevant changes also run **Playwright UI-E2E (mock)**, split into three parallel jobs named `1/3` to `3/3`, and **Mock fidelity verification**.

For a PR from a fork, the **CLA** check asks each contributor to accept the [Contributor License Agreement](../../CLA.md). Follow the exact comment and commit-email instructions in [`CONTRIBUTING.md`](../../CONTRIBUTING.md#contributor-license-agreement).

## Read the review

CORSIA itself runs no pull request review. A project that installs the review workflow from CORSIA's community gets the review from that workflow, which reads GitHub on its own and posts two commit statuses on the PR's head:

- `qa/llm-reviewer` is the verdict. It is `pending` while the review waits (for checks, a maintainer or a conflict to clear) or is requested, `success` when nothing blocks or a maintainer skipped the review, `failure` when it found something that must be fixed, and `error` when the review could not finish and will retry.
- A second status, named like the first with a `-present` suffix, is `success` once a review for that head is published.

Without the workflow installed, neither status appears.

- A finding is evidence, not authority: verify it against the current code, then fix it or explain why it does not apply.

Checked against the code at `702e54716`.
