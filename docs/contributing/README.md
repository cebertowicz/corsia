# Contributing to CORSIA

The path from a fresh clone to a merged change, for developers who want to change CORSIA's code or docs. The rules for issues, claims, the Contributor License Agreement and pull requests are in [`CONTRIBUTING.md`](../../CONTRIBUTING.md); these pages cover how to do the work.

## Reading order

| Step | Read | To learn |
|---|---|---|
| 1 | [`CONTRIBUTING.md`](../../CONTRIBUTING.md) | Which issues are open to you, how to claim one, the CLA, and what a pull request needs |
| 2 | [How CORSIA works](../how-it-works/README.md) | The parts of the system and the terms used everywhere else |
| 3 | [`ARCHITECTURE.md`](../../ARCHITECTURE.md) | Where each part lives in the code |
| 4 | [`BACKEND.md`](../../BACKEND.md) or [`FRONTEND.md`](../../FRONTEND.md) | Rules for the side you are changing |
| 5 | [Writing docs](writing-docs.md) | Where a page goes, the format, and how to keep it current |

## Before you change anything

- Work on a feature branch based on `dev`, and open your pull request to `dev`.
- Never run tests or experiments against the data of a CORSIA you use. Use a second checkout with its own data.
- Change the docs in the same pull request as the code they describe.

Checked against the code at `7a8c55d84`.
