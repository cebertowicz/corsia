# Cross-workspace coordination protocol

**Status:** current · **Owner:** CORSIA maintainers · **Review:** after the first ten contributor PRs

**Audience:** agents in a contributor's own CORSIA workspace that donate
work to the upstream CORSIA repository. Load this document into the
contributor agent's bootloader pointers. Read it before picking up, designing,
or opening anything upstream.

## 1. Two workspaces, one shared surface

| | Upstream (maintainers) | Contributor workspace |
|---|---|---|
| Owns | Product direction, design, lane ownership, merge, release, CI and reviewer gates | Execution of accepted issues; its own agents, plans, and data |
| Shared surface | The GitHub repository: issues, PRs, comments, labels | Same |
| Never shared | Threads, plans, knowledge base, workspace files, runtime state, user data | Same |

The GitHub repository is the only channel. Agents in one workspace never
message, call, or read the other instance. The upstream lane owners keep
design ownership of their domains. A contributor agent executes approved
work inside a domain and does not decide product behavior, architecture
direction, or scope for it.

## 2. What is safe to pick up

An issue is **eligible** only when **all** of these hold:

1. It is open, and nobody is assigned or has left a claim comment in the last
   14 days.
2. It carries **`contrib-ok`**, the label maintainers apply to issues they
   are willing to accept from outside. Contributors never apply it. An issue
   without it is not open to you, however small it looks.
3. It carries a `lane:*` label, which names the design owner you will address
   in §3.
4. It is a reproducible `bug`, an `enhancement` with a stated success
   signal, a `documentation` fix, or an `eval-proposal`, **or** it is a
   `feature` or `discussion` whose design a maintainer has already approved
   on the issue.
5. It changes no product behavior beyond what the issue already approves, and
   touches no schema or migration, security or authorization,
   release/CI/reviewer machinery, or production operations, unless the issue
   says otherwise in a maintainer's comment.

Everything else is **not eligible**. Leave a comment if you have evidence to
add. Never open a PR for it.

**Claiming.** Comment on the issue with `Claiming: <one-line plan>` before
starting. If a maintainer or another contributor objects, stop. Release the
claim with a comment when you stop.

**Tie-break.** Two contributors can claim the same issue at the same moment.
After posting your claim comment, re-read the issue: the earliest claim
comment wins. If yours is not the earliest, withdraw it with a comment and
pick another issue. Re-check the issue for a competing claim or an assignee
once more immediately before opening the PR.

## 3. Design before implementation

For anything beyond a mechanical fix, post a **design proposal** on the issue
and wait for a maintainer's explicit approval comment before writing code.
Use the `decision-brief` shape, and put the long form in a collapsed
`<details>` block:

- the outcome and behavior change, in user terms;
- the root cause, with evidence (file and function);
- the approach and its development-doctrine category
  (`starter-kit/shared/standards/DEVELOPMENT-DOCTRINE.md`), plus the
  complexity ledger for category 4 or 5;
- affected callers, states, and tests (the impact inventory from
  `sdlc-process`);
- open questions for the lane owner.

**Approval signal.** A maintainer applies the **`contrib:design-approved`**
label, normally with a comment that says what was approved. Check the label,
not your reading of a comment. Contributors never apply it. Silence is not
approval.
If a reviewer or maintainer later redirects the design, the new direction
replaces the old one.

## 4. Pull request conventions

Follow `project-skills/sdlc-process` (contributor edition) and
`skills/github`, with these cross-workspace rules:

- **Base:** `dev`. Never `main`.
- **Branch:** `contrib/<issue number>-<slug>` (this keeps
  contributor branches out of maintainer namespaces). Create it with
  `worktree_create` in your `corsia-contrib` project.
- **Title:** `[<contributor agent name>] <behavior change>`, capped at 32
  characters inside the brackets, as in the `github` skill.
- **Body:** the repository's `.github/pull_request_template.md`, plus this
  ownership block instead of a thread ID. Thread IDs from your instance mean
  nothing upstream.

  ```text
  Owner: external contributor workspace — <contributor agent name>
  Issue: #<number> (design approved: <link to approval comment>)
  ```

- **CLA:** the GitHub account behind each commit accepts `CLA.md` once, as
  `CONTRIBUTING.md` describes. Post the acceptance comment
  only after the account's owner has read `CLA.md` and told you to accept
  it; it binds them. A label, an earlier acceptance elsewhere, or silence is
  not that instruction. The check finds each commit's account from its author
  email; CORSIA already commits as the connected agent account's no-reply
  address, so there is nothing to set.
- **Scope:** one issue per PR. Include `Closes #<n>` only when the PR fully
  resolves the issue.
- **Evidence:** link the approval comment and give targeted test results.
  Never run the backend full suite locally; upstream CI owns it.
- **Gates:** upstream CI and `qa/llm-reviewer` decide. `qa/llm-reviewer`
  judges every PR against the development doctrine, the engineering &
  architecture, product, and UX/IA standards, and the LLM anti-pattern
  catalog, all in `starter-kit/shared/standards/` — read them before
  designing. Never set, re-post, or override a `qa/*` status or label, and
  never push an empty commit to retrigger a run. Reply to each finding on
  its thread. The stop rules in `sdlc-process` apply unchanged.
- **Merge:** maintainers merge. Contributors never merge, even with
  permission to do so.
- **Events:** register the PR as an artifact in your own instance
  (`integration: github`, `type: pull-request`,
  `external_id: <owner>/<repo>#<n>`). Your instance receives no upstream
  webhooks, so use one bounded cadence automation per open PR (at most
  twice daily, `max_runs` ≤ 20) that reads the PR state with `gh pr view`.
  This is the only exception to the "never poll" rule, and it ends when
  cross-instance event delivery exists.

## 5. No private data crosses workspaces

Before any issue, comment, or PR is created or edited, read the complete
outgoing text, attachments, and links, and remove:

- names of people, private paths, hostnames, account identifiers, emails,
  tokens, and keys;
- content copied from your threads, plans, knowledge base, or user data;
- attribution of private feedback. Never quote, name, or attribute a private
  user or their feedback without explicit permission for that attribution and
  audience. Phrases like "users report" and "user feedback" still reveal the
  source. State problems, evidence, and requirements impersonally.

Logs and screenshots are redacted before upload. Redaction is not a reason to
skip the evidence. Describe what you removed.

## 6. What to report upstream, and how

Report these as issues, following `skills/backlog-intake`:

| Report | Type | Include |
|---|---|---|
| Setup and install friction from `OFFLINE-BOOTSTRAP.md` | `documentation` or `bug` | The step number, what happened, the fix you used |
| Agent-experience (AX) gaps: a confusing tool schema, a misleading error, a missing affordance, context the agent needed but did not get | `enhancement` | The agent's journey step (see `skills/agent-experience`), the exact text it saw, the model and harness |
| Tool errors and crashes | `bug` | Tool name, arguments shape (redacted), exact error, commit |
| Product bugs | `bug` | Reproduction on the current `dev` head |

**Search first.** Run
`gh issue list --search "<terms>" --state all` with the exact error words
and two synonyms. If an issue exists, **comment on it** with your new
evidence (commit, environment, reproduction) instead of filing a duplicate.
Do not add +1 comments that carry no evidence. File a new issue only when
nothing matches, and leave the lane label choice to the maintainers if the
owner is unclear.

## 7. When the protocol does not answer

Ask on the issue and wait. Do not infer permission from a label, a similar
past PR, or silence.
