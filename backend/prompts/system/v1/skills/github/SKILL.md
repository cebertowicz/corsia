---
name: github
description: Use when working with GitHub from any thread — opening or updating a pull request, reading PR/CI/review status, merging, commenting on PRs or issues, or any repo operation. Load before creating a PR so its events route back to you.
---

# GitHub (via the `gh` CLI)

When `model_sources` reports `github_agent` other than `not_installed`, agent shells act as the agents' own GitHub account; read [the agents' account procedure](references/agent-account.md) before connecting, recovering, or using it. Never fall back to the person's own `gh` login while an agents' account exists. With `not_installed`, shells use the person's own setup, including when they chose to post as themselves.

Use the installed, authorized `gh` integration. If it is unavailable, record the integration gap and discover a supported alternative before proceeding; do not invent a credential/raw-HTTP workaround or claim the action occurred. CORSIA may observe recognized commands, but that is best-effort; every owned PR needs verified canonical registration for configured CI, review and lifecycle routing. Registration does not establish that the event source is configured or healthy.

## Opening a PR

```bash
gh pr create --base <project-approved target> --head <branch> --title "<behavior change>" --body-file <body-file>
```

- Register or verify the PR immediately as an artifact: integration `github`, type `pull-request` (hyphen), external id `<owner>/<repo>#<N>`. Registration is idempotent; any other spelling will not route.
- Resolve the project's current integration target before creating the PR; the repository default may be promotion-only or production. Follow any active train/stack policy.
- Give it a real title and body; the title becomes the artifact label.
- Follow the project's title and ownership format. Identify the accountable owner thread by full name and exact thread id in the body, taken from current thread context, not the shared GitHub login or a guess. When a temporary worker owns the PR, name the worker and the supervising thread. Keep that block when rewriting the body. Write multiline bodies to a file and use `--body-file`.

## Common operations

```bash
gh pr view <N> --json headRefOid,statusCheckRollup,state,mergeable,reviewDecision
gh pr diff <N>
gh pr merge <N> --squash      # your own green, reviewed PR, under the project's merge gate
gh pr comment <N> --body "..."
gh api repos/<owner>/<repo>/pulls/<N>/comments
```

- Use configured PR events instead of polling. If delivery is unavailable or overdue, inspect the route once and establish a supported bounded wait; do not assume registration alone guarantees a wake.
- Before merging, take one snapshot of `headRefOid`, checks, `state`, `mergeable`, and `reviewDecision`. Classify each red as author defect, regression from a fix, reviewer or configuration noise, or infrastructure. Change code only for a real in-scope defect; disposition noise and infrastructure under the project's gate.
- Reconfirm `headRefOid` immediately before merging; a changed head voids the earlier snapshot.
- When reviewing, read the diff and reply on each thread; resolve only when settled.
- Never commit to a protected branch directly, and do not build a PR-creation wrapper; the CLI plus this skill is the integration surface.
- If GraphQL is rate-limited, REST may be used with the same merge gate and expected-head SHA; never drop the revision guard in a fallback.
- Publishing, commenting, or merging is an external effect. Do it when the work you were asked to do includes it; otherwise draft and stop.
