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

The LLM reviewer examines the diff against the project's standards and any mapped approved requirements, including security and performance.

- **qa/llm-reviewer** reports the verdict and counts of findings. A **BLOCK** is a must-fix finding: verify it against the current code, then fix it or explain why it does not apply. It prevents a passing verdict. **Different approach needed** means several findings share one cause: fix that cause, not each finding on its own.
- **FLAG** is a possible problem that warrants attention; **NIT** is a low-risk suggestion. Both are diagnostic evidence, not automatic requests to change a clean candidate.
- **qa/llm-reviewer-present** succeeds only once a verdict is published for that head; it does not say whether the verdict passed. Before that it can show pending, for example while an approved fork head waits for its checks. A pending or error status is not a clean review.

Findings appear in review threads when they can be anchored to a line, or as PR-level comments otherwise. Reply on the finding thread with the fix or your evidence if you disagree. On a later review, a finding that is no longer raised receives a reply saying so; that does not by itself prove the original concern was fixed.

Checked against the code at `310115261`.
