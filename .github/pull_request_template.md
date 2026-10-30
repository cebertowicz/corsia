## Scope

<!-- State the current authorization and the exact in/out fence. Reviewer findings do not expand it. -->

- Authorized by:
- In:
- Out / parked:
- Approved PRD + acceptance criteria, or `no-contract-change: <reason>`:

## Change

<!-- What changed, why this is the smallest correct root-cause fix, and the named state owner for stateful work. -->

## Release note

<!-- A PR that changes what a person sees or does adds one plain line under ## Unreleased in CHANGELOG.md in this PR, in alphabetical order, never at the top or the end. Internal-only PRs write none. -->

- Line added under Unreleased (or none):

## First-pass evidence

<!-- Link author-time evidence where it already lives; do not transcribe it into the PR. -->

- Work object containing current author-time evidence:
- Relevant tests/build/browser proof:
- Same-criteria local review evidence (SHA-bound preflight receipt when available):
- Fresh-eyes refutation:

## Review attribution

<!-- On follow-up heads: author first-pass defect, fix regression, reviewer noise, or infrastructure error; include evidence. -->

- First reviewable head:
- Prior reviewed head:
- Disposition:

## Risk and rollout

<!-- Failure/transition matrix for stateful work; migration, rollback, and release impact where applicable. -->

- Scale budget (request path, list, stream, store, or background job; else `n/a: <reason>`): queries per request, worst-case rows read for the largest parent at current production volume and at 10x, and the load-time target with how it was measured before and after:
