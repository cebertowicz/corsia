---
name: sdlc-process
description: Use for PR-bearing code work from product intent through UX/IA and engineering — author-time scope gates, existing-surface reuse, worktree discipline, first-pass proof, review, and merge follow-through.
corsia:
  handler:
    kind: instruction
---

# SDLC process

> Contributor edition. In a contributor workspace, "the maintainers" are the upstream project's owners and lane owners. Product and design approval always comes from them, on the issue, before implementation (see `CROSS-WORKSPACE-PROTOCOL.md` in the bootstrap pack). You never merge; the maintainers do.

## The three stages happen before code

Every change that affects product behavior moves through the same three stages. Scale the evidence to the change—a one-line bug fix does not need a large PRD—but no stage may disappear or be postponed until review.

1. **Product:** Name the current approved outcome, user need, and complete behavior delta. Inspect the product as it exists now and confirm the need is not already met. If the outcome is not yet approved or the need is not yet framed, run `product-thinking` and record its chain in the work object; that record is the Product-stage evidence, and a design proposal for the maintainers is posted on the issue as a `decision-brief` that links it. Before calling existing behavior defective or planning its reversal, complete the **Establish intent before calling behavior defective** check in `engineering-standards` and record whether the behavior is deliberate, contradicted by current authority, or still unknown. Agents may decide implementation details autonomously only when they preserve the approved user experience, mental model, scope, ownership boundaries, and extensibility direction; add no material ongoing complexity; and do not alter another product area. Otherwise—or when the boundary is uncertain—the maintainers' approval on the issue is required before implementation.
2. **UX/IA:** For anything a user sees, clicks, receives, or must understand, inspect the fully composed current surface before implementation. Use `frontend/DESIGN-SYSTEM.md` and the live `/_design` reference; ask on the issue for the maintainers' UX/IA standards when the change needs them. Identify the existing pattern/components to reuse, the information hierarchy, surrounding states, and what the change replaces. A user-facing design also gets rendered mockups, walked through the 3–5 main user journeys it touches and approved before build by the lane that owns the composed experience (the owner if none); the build task depends on that approval, the conductor names another lane to review that lane's own designs, and the owner approves behavior on the rendered design and settles any disagreement. In a contributor workspace the maintainers give that approval on the issue. A tiny change may record the rest of this in a few lines in the work object; missing browser access is a blocker, not an exemption.
3. **Engineering:** Only after Product and applicable UX/IA are settled, design the technical change, classify it under the development doctrine (`shared/standards/DEVELOPMENT-DOCTRINE.md`), implement the simplest root-cause fix, and verify it through tests, review, and the real user surface. A category-5 mechanism records why deletion, making the bad state unrepresentable, reuse, or consolidation cannot solve the problem and what greater complexity it buys down.

The author owns all three stages within the domain. Do not hand Product or UX judgment to a late code reviewer, and do not treat CI, component tests, or a clean diff as evidence that the product decision or composed experience is correct.

### End-to-end design before runtime changes

Before changing runtime code, complete an evidence-backed end-to-end design in the work object:
- Start from the existing requirements and documentation.
- Trace every affected entry point, provider, caller, state owner and downstream consumer through success, failure, cancellation, restart and the next useful user outcome.
- Reproduce the critical assumptions with isolated, non-billed tests. Never mock the integration boundary under question with a helper.
- Document the smallest complete repair, the exact file and test scope, the reachable failure exits, and the effects on behavior, cache, cost and compatibility.
- Settle technical design questions yourself before asking for approval; bring the maintainers only real product tradeoffs, on the issue.
- Keep one canonical documentation source.

Implement only once this gate is met. PR review is not where the architecture gets discovered.

### Carry Code Ground through delivery

Complete the `engineering-standards` Code Ground before activating implementation and record it in the existing work object. Reconfirm it against the current tree before the first push, then link that work object from the PR's **First-pass evidence**. Keep the evidence current at its owner; do not transcribe its fields into the PR or create a parallel checklist, plan type, or schema.

### Impact inventory before implementation

When a change moves, renames, removes, or changes a consumed path or identifier, event or schema contract, state owner, lifecycle, or persisted/runtime shape, record its impact inventory in the work object before implementation. Search exact names and semantic variants across source, configuration, tests/fixtures, documentation, skills, bootloaders, migrations, and release surfaces. Cover direct callers/readers/writers, sibling flows, compatibility and prior data, rollout/release effects, and coordinator context. Mark each consumer **migrate**, **verify unaffected** with evidence, **retire**, or **held for scope**. An unknown required consumer blocks implementation; discovery never authorizes out-of-scope edits.

### Implementation admission for risky changes

Run this once per approach, before activating implementation, when either holds: the change adds lifecycle, cleanup, repair, sweep, migration, or notification machinery for state it does not create; or it is doctrine category 4 or 5, introduces or replaces a state owner, crosses a transaction boundary, or enters another lane's declared code surface (the upstream lane labels and the owners named on the issue). Record four items in the existing work object: **Producer** — the exact file and function that create the state; **Deletion test** — why deleting or changing the producer is not the fix; **Owner** — the owning lane, any in-flight work on that surface, and one outcome: `own-it`, `routed`, or `owner-approved`; **Approach proof** — doctrine category, complexity ledger when required, and the falsification sentence, timestamped before the first implementation commit. A missing or conflicting item holds implementation. The gate reopens only when the producer, owner, or doctrine category changes; it does not rerun per review round.

### Protect accepted evidence

Once a test, fixture, baseline, or assertion is accepted as the proof of done, the implementer must not edit, weaken, delete, or bypass it to make their own work pass. If current requirements show that the proof target is genuinely wrong, stop and return the conflict to the accountable owner; only that owner may approve changing the target. A green result obtained by weakening accepted evidence is failure. Fail-first counts only when the unchanged proof reaches the required seam and fails for the intended behavioral reason; setup, environment, or unrelated failures do not count.

## Scope + workspace
- One accountable owner writes a coupled mutable change. Delegate only self-contained work with a disjoint/frozen boundary, read-only evidence gathering, or independent verification; the owner retains the plan and can actually verify every return.
- The current scope fence is binding. A later direct user narrowing supersedes an older plan/PRD group; update the durable scope/AC ledger and park exclusions before more mutation.
- Work in a fresh per-task worktree. Before installing dependencies or running any check, load `corsia-test-environment`; it owns interpreter, dependency, state, cache, port, test-tier, and environment-failure procedure. Run tests, builds, and preview/E2E only through those supported entry points. Before every commit/push, verify `git status` contains only intended files; never commit-all around unrelated changes.
- **Infrastructure failure is not missing authority.** Before requesting any host-access lease, classify the failure. A `.git/**/index.lock` error, a refused redeploy, a stale base-checkout answer, a bridge or checkpoint error, or a tool that fails identically under an active lease is a product defect or environment fault: report the exact error, file or update the owning lane's issue, and continue with what does not depend on it. Do not request a lease for it, and never request a policy exception for a routine worktree, commit, or plain `git push origin <branch>`.
- Before choosing commit or PR boundaries, classify the change as **additive** or **replacement**. Additive work leaves the existing concept and state owner intact; replacement work intends to retire them.
- For replacement work, use intermediate commits in one worktree/branch, but do not open a PR until the legacy owner/path is removed and the replacement is coherent across the approved surfaces. A locally testable slice is not a valid PR boundary when it preserves two truths only for iterative or review convenience.
- Coexistence is allowed only for an explicit compatibility or rollout need. Before the first push, name each path's owner and routing rule, the bounded retirement condition, migration/rollback proof, and the added code, test, reviewer, and token cost; explain why two paths are unavoidable.

### Creating a worktree
1. Preflight: inspect `git worktree list` in the base repo. If a path already carries another thread's ID and that thread is still live (`delegate_status`), do not touch it—pick your own.
2. For every new task branch, call `worktree_create` once with `repo_name`, `branch`, and a short `descriptor`. Omit `base_branch` for a branch-pinned repository; otherwise provide the intended remote base branch. Specify `remote` only when it is not `origin`.
3. The operation fetches the configured remote, resolves the exact remote base SHA, creates `.worktrees/<thread-id>-<descriptor>`, and returns the created path and SHA in its receipt.
4. Continue only in the returned `worktree_path` and retain `base_sha` in the task's work state.
5. Do not run separate shell commands to fetch or create a new task branch worktree. Raw `git worktree add` remains for existing-branch reattachment and unprotected manual workflows; protected-base new-branch creation is mechanically rejected.

### Git safety
- Banned, no exceptions: `git reset --hard`, `git clean`, `git stash`, checkout/restore over uncommitted changes, and any force-push — they destroy unrecoverable work. If one seems necessary, stop and surface it instead.
- Heavy test runs (full suite, E2E, parallel loops) are a shared host-capacity boundary. Worktree agents run only the smallest affected tests and never start the backend full suite locally. CI owns one full-suite result per exact PR head; reproduce any failure with targeted tests before pushing a new head. Never add runner processes or pytest workers to go faster.

### Mechanical git traps (verified against live enforcement)
- **One literal git command per call, never chained (`&&`, `;`, pipes).** The sandbox guard only recognizes a single literal direct `git ...` invocation; a chained command loses its grant and fails with a raw OS-level "Operation not permitted" instead of the guard's real guidance. If a git step fails oddly, retry it alone before assuming it's broken.
- **`git push -u`/`--set-upstream` fails in a linked worktree** — it needs to write the shared `.git/config` in the protected base checkout, which is blocked. This is expected, not a bug: run `git push origin <branch>` (no `-u`), then `gh pr create --head <branch> --base <target-branch>` explicitly — PR creation does not need local upstream tracking.
- **A stray local branch ref left after `git worktree remove` cannot be deleted from the base checkout** (`git branch -D` there is blocked by the same base-checkout protection, even though it doesn't touch the working tree). Harmless clutter, not a blocker — leave it; do not attempt workarounds against the base checkout to force cleanup.

## First-pass-clean gate — before the first push
The reviewer confirms clean work; it is not the first defect detector.

1. Confirm the PR names its Product source/approval and, for user-facing work, the UX/IA evidence. Reconfirm the work object's Code Ground against the current tree: existing owners and reused paths, affected callers/siblings/contracts, state that must survive, and failure ownership/visible state. For stateful work, reconfirm the single state owner and transition/failure model. It must also name additive vs. replacement; replacement work proves the legacy owner/path is gone or includes the justified coexistence packet above. When impact inventory is required, link its work object and confirm every required consumer has a disposition. Missing or unresolved author-time evidence holds the push.
2. Using `corsia-test-environment`, run the relevant local tests/build/browser proof and classify setup/host failures separately from source failures. **Test the way the user experiences the change:** if the change alters anything a user sees, clicks, or receives (UI, error text, denials, chat-visible behavior, API responses the frontend renders) — verify it through that real surface (browser/live flow), not only unit tests. "I didn't touch UI files" does not exempt a change whose *effect* reaches the user.
3. Run the real reviewer criteria over the diff. Read `backend/prompts/reviewer.py`, `backend/reviewer/on_pr_review.py` (`_build_context`), and `backend/reviewer/reviewer_runner.py` in the repository, and run that review with a fresh-context subagent. Use exactly the context actually resolved; do not claim missing PRD/AC/standards evidence.
4. Fresh-eyes refute edge cases, failure modes, transitions, and affected callers. For generated, sharded, automated, or reporting flows, name the authoritative inventory/configuration and reject duplicates. For non-gating or always-run observers, exercise success, partial, empty, missing, and malformed inputs; failures stay visible without changing the primary result.
5. Under a labeled **"First-pass evidence"** section in the PR description, link the existing work object that owns the current Product, UX/IA (or concrete reason not applicable), Code Ground, development-doctrine classification, the complexity ledger for a category-4 or category-5 change, and required impact inventory. Add the tests/build, self-review, and refutation results that are not already durable there. Do not transcribe the work object's fields into the PR. A PR without this evidence link is indistinguishable from one where the gate was skipped—reviewers and auditors treat its absence as gate-not-run. A manual checklist is weaker manual review and must be labeled as such.

Do not push merely to see what CI/review says. Do not add assertions, comments, or behavior solely to appease a predicted false positive.

## Attribute results before acting
- **Author first-pass defect:** a real block/flag present on the first reviewable head, including one found later on behavior unchanged since that head.
- **Fix regression:** a new real block/flag introduced by the delta from the prior reviewed head to the revised head.
- **Reviewer noise:** an explicitly settled prior finding re-raised with no new affected behavior or contract. Disposition it; do not churn code.
- **Infrastructure error:** provider, parser, stale-head, webhook, status/comment projection, or owner-delivery failure. Keep it red/retryable; it is not a code verdict.
- **Nit:** advisory. Reply/disposition; never re-push only for a nit.

When uncertain whether a finding is real, investigate it as real. Resolve disputes through `decide-or-raise`: bind the claim, governing requirement, reproduction/refutation, and disposition to the current head. Once current-head evidence refutes a finding, do not edit or push source; a commit that exists to turn the badge green is churn, not a fix.

**Know what can clear it.** The reviewer runs at most once per head SHA (skip-guard, `backend/events/reviewer_gate.py`), and the next run reads prior findings only as `SEVERITY [category] path` rows (`build_review_history`) — your reply is never in its input. So no argument you write changes a verdict, and the status on an already-reviewed SHA never recomputes. Only a new head, or a status posted on that SHA, moves `qa/llm-reviewer`. Do not re-push, re-run, or re-reply hoping for a different result.

**Refute terminally instead:** reply on the finding thread with the head SHA, the governing requirement, and the reproduction that failed; record the disposition on the work object; hold the PR; and ask the maintainers for a waiver with that evidence. A waiver request carrying a bound refutation is not liability transfer — asking him to override a finding you did not investigate is, and stays forbidden. A still-red or missing gate remains a hold, not permission to bypass it. The `qa/override-reviewer` label discards every finding on the PR at once; applying it is the maintainers' call, never the author's.

## Stop rules
- **What counts:** only a new substantive block/flag on a distinct candidate counts as a failed LLM review. Infrastructure errors, same-SHA reruns, nits, and settled duplicates/noise do not count or trigger escalation.
- **Local:** pre-push review has one job: confirm the candidate implements the admitted approach and carries first-pass evidence. An approach-level finding (state owner, producer, mechanism, boundary) returns to implementation admission; it is never patched inside another private full-diff round. Every private substantive round counts toward these rules. After two counted local reviewer failures, stop editing and run the wide-lens approach review below before another local candidate. Do not push a design that still fails materially just to obtain remote feedback.
- **Remote wide-lens hold:** after the second counted revised-head failure on a PR, stop editing, pushing, and automatic review reruns. Do **not** escalate to the maintainers yet.
- **Approach review:** re-derive the approved outcome and intent evidence; group all findings by root cause; inspect callers, sibling flows, states, contracts, and the proposed fix's full blast radius; reconsider the state owner, abstraction, PR boundary, and whether the reviewer's remedy is wrong or a better solution exists. Record the resulting architecture verdict, one named state owner plus transition/failure matrix when stateful, and the revised test-first plan.
- **One revised attempt:** after that approach review, push exactly one coherent revised candidate without asking the maintainers; a further private candidate requires re-running implementation admission. Another counted substantive failure holds implementation and pushes. Do bounded investigation or independent refutation under `decide-or-raise`; resume only with an evidenced revised approach inside existing authority. Raise only a remaining reserved decision, with investigation cost weighed against user attention. Nits, infrastructure errors, same-SHA repeats, and settled noise are dispositioned or retried normally.

Until lifecycle enforcement is mechanical, the author enforces the hold manually.

## Feedback + scope ceiling
For every LLM-review finding, analyze the full blast radius—callers, sibling flows, states, contracts, and the consequences of the suggested remedy—before editing. The reviewer may reveal a better solution, or its proposed fix may be wrong even when the finding is valid. Analysis may block the current approach or request re-scope; it never authorizes adjacent implementation. If correctness requires crossing the scope fence, hold the PR and ask the user rather than patching outside scope.

## Merge gate
For `dev`:
- Product approval/source is current, and user-facing work has author-time UX/IA evidence plus real-surface proof;
- current-head CI green and mergeable;
- confirmed current-head reviewer pass;
- no open block;
- every flag fixed, explicitly dispositioned, or routed as approved follow-up — **before the merge lands, never as post-merge cleanup in a follow-up PR**;
- nits advisory.

For `main`: zero open blocks; zero open flags unless explicitly steward-waived; CI green; mergeable; normal release gate.

Contributors never merge: the maintainers merge a green, approved `dev` PR. Never use `--admin` to bypass red/missing state. Missing current-head review is maintainer-owned infrastructure: comment once on the PR with the head SHA; never push an empty commit or no-op change to retrigger it.

## Event discipline
Register every PR to its owning thread. Never poll or sleep-loop for review, CI, comments, or mergeability; the event router resumes the owner. If an expected event, projection, or owner wake does not occur, treat it as an infrastructure defect: preserve the concrete evidence and file or update a GitHub issue instead of substituting polling.
