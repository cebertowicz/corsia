# Contributing to CORSIA

Thank you for contributing. Public contributions must contain only material you may publish; do not include private workspace content, credentials, personal data, or internal deliberation.

## Before you start

Follow the developer path in [`docs/contributing/`](docs/contributing/README.md). Read [`AGENTS.md`](AGENTS.md) and the relevant parts of [`ARCHITECTURE.md`](ARCHITECTURE.md), [`BACKEND.md`](BACKEND.md), or [`FRONTEND.md`](FRONTEND.md). Work on a feature branch based on `dev`, in a checkout set up as in [Developer setup](docs/contributing/dev-setup.md). Never run tests against your real CORSIA's data.

## Contributor License Agreement

Before your first pull request from a fork can be merged, you accept the [CORSIA Contributor License Agreement](CLA.md) once for your GitHub account. A bot comments on the pull request and a check named **CLA** fails until you post this exact comment:

```text
I have read the CORSIA CLA and I accept it
```

An agent may post it for the account it runs under only after that account's owner has read the agreement and told it to accept; the owner is then bound by it. The bot identifies who wrote each commit from the commit's email address, so author your commits with an email linked to your GitHub account (your `<id>+<login>@users.noreply.github.com` address always works). Comment `recheck` to run the check again.

## Issues

For anything beyond a mechanical fix, discuss the approach on the issue before implementation; maintainers decide whether a change fits the project's scope.

Use `dev` as the base, a `contrib/<issue>-<slug>` branch, and one issue per PR. Use `Closes #n` only when the PR fully fixes the issue. Do not change `qa/*` statuses or push empty commits; maintainers merge.

Do not put private data in issues or PRs: names, private paths, hosts, account IDs, tokens, or copied private content. Redact logs and screenshots, and say what was removed.

Search issues before filing. Add new evidence to an existing issue instead of opening a duplicate.

## Pull requests

Open a public pull request to `dev` from your feature branch. Explain the user-visible or technical change, include focused tests, and keep the PR within its stated scope.

If your PR changes what a person sees or does, add one plain-language line under `## Unreleased` in `CHANGELOG.md`, in its alphabetical place (not simply at the top or the end), describing the change as a user would notice it. If it doesn't (tests, refactors, CI, internal docs), write `none` under **Release note** in the pull request description.

Before pushing, test your change and run the local gate as [Testing and review](docs/contributing/testing.md) describes; that page also explains the checks and the review your pull request gets. For system design and runtime boundaries, use the architecture documents above.

Checked against the code at `310115261`.
