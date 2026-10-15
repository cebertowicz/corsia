# Contributing to CORSIA

Thank you for contributing. Public contributions must contain only material you may publish; do not include private workspace content, credentials, personal data, or internal deliberation.

## Before you start

Read [`AGENTS.md`](AGENTS.md) and the relevant parts of [`ARCHITECTURE.md`](ARCHITECTURE.md), [`BACKEND.md`](BACKEND.md), or [`FRONTEND.md`](FRONTEND.md). Work from a feature branch in a linked worktree based on `dev`; do not develop in a serving checkout or test against an operational database.

## Contributor License Agreement

Before your first pull request from a fork can be merged, you accept the [CORSIA Contributor License Agreement](CLA.md) once for your GitHub account. A bot comments on the pull request and a check named **CLA** fails until you post this exact comment:

```text
I have read the CORSIA CLA and I accept it
```

An agent may post it for the account it runs under only after that account's owner has read the agreement and told it to accept; the owner is then bound by it. The bot identifies who wrote each commit from the commit's email address, so author your commits with an email linked to your GitHub account (your `<id>+<login>@users.noreply.github.com` address always works). Comment `recheck` to run the check again.

## Pull requests

Open a public pull request to `dev` from your feature branch. Explain the user-visible or technical change, include focused tests, and keep the PR within its stated scope.

Before pushing, run the local gate:

```bash
bash scripts/check-pr
```

Also run the targeted tests for the files and behavior you changed. For frontend changes, use the applicable commands from `frontend/package.json`, such as `npm run check` and `npm run test`; for backend changes, run the affected tests with the repository's Python 3.11 environment. Do not run the backend full suite locally: CI owns that suite.

Every pull request reports the required check contexts. When a job is irrelevant to the changed files, its context is reported as skipped rather than omitted.

For browser and end-to-end test guidance, see [`frontend/e2e/README.md`](frontend/e2e/README.md). For system design and runtime boundaries, use the architecture documents above.
