# Contributing to CORSIA

Thank you for contributing. Public contributions must contain only material you may publish; do not include private workspace content, credentials, personal data, or internal deliberation.

## Before you start

Read [`AGENTS.md`](AGENTS.md) and the relevant parts of [`ARCHITECTURE.md`](ARCHITECTURE.md), [`BACKEND.md`](BACKEND.md), or [`FRONTEND.md`](FRONTEND.md). Work on a feature branch based on `dev`. You can make it in your CORSIA checkout at `~/.corsia` and edit there: `corsia restart` serves your edits, uncommitted ones included. Commit or stash them before **Settings → System → Update now**, which refuses while the checkout has uncommitted edits. Try a risky change, such as a migration, on a second checkout with its own data first (see the README's everyday commands). Never run tests against your real CORSIA's data.

Try your change in the browser. The desktop app does not run your checkout's backend, even under `tauri dev`, and a desktop app that has taken over your source install stops it from serving your checkout.

## Contributor License Agreement

Before your first pull request from a fork can be merged, you accept the [CORSIA Contributor License Agreement](CLA.md) once for your GitHub account. A bot comments on the pull request and a check named **CLA** fails until you post this exact comment:

```text
I have read the CORSIA CLA and I accept it
```

An agent may post it for the account it runs under only after that account's owner has read the agreement and told it to accept; the owner is then bound by it. The bot identifies who wrote each commit from the commit's email address, so author your commits with an email linked to your GitHub account (your `<id>+<login>@users.noreply.github.com` address always works). Comment `recheck` to run the check again.

## Picking up an issue

An issue is open to outside contributors only when a maintainer has applied `contrib-ok`; contributors never apply that label.

Eligible issues have a lane label and are a reproducible bug, an enhancement with a success signal, documentation, an evaluation proposal, or a feature whose design a maintainer approved. Schema, security, release, CI, reviewer machinery, and production operations require a maintainer to say they are eligible.

Claim an eligible issue with a `Claiming: <plan>` comment; the earliest claim wins. Release the claim when you stop. A claimed issue belongs to its claimant, and maintainers do not start it in parallel.

For anything beyond a mechanical fix, propose the design on the issue before implementation. Only a maintainer-applied `contrib:design-approved` label approves it; contributors never apply that label, and silence is not approval.

Use `dev` as the base, a `contrib/<issue>-<slug>` branch, and one issue per PR. Use `Closes #n` only when the PR fully fixes the issue. Do not change `qa/*` statuses or push empty commits; maintainers merge.

Do not put private data in issues or PRs: names, private paths, hosts, account IDs, tokens, or copied private content. Redact logs and screenshots, and say what was removed.

Search issues before filing. Add new evidence to an existing issue instead of opening a duplicate.

## Pull requests

Open a public pull request to `dev` from your feature branch. Explain the user-visible or technical change, include focused tests, and keep the PR within its stated scope.

If your PR changes what a person sees or does, add one plain-language line under `## Unreleased` in `CHANGELOG.md`, describing the change as a user would notice it. If it doesn't (tests, refactors, CI, internal docs), write `none` under **Release note** in the pull request description.

Before pushing, run the local gate:

```bash
bash scripts/check-pr
```

Also run the targeted tests for the files and behavior you changed. For frontend changes, use the applicable commands from `frontend/package.json`, such as `npm run check` and `npm run test`; for backend changes, run the affected tests with the repository's Python 3.11 environment. Do not run the backend full suite locally: CI owns that suite.

Every pull request reports the required check contexts. When a job is irrelevant to the changed files, its context is reported as skipped rather than omitted.

For browser and end-to-end test guidance, see [`frontend/e2e/README.md`](frontend/e2e/README.md). For system design and runtime boundaries, use the architecture documents above.
