# README evidence and limits

This record explains the limited benchmark and operational aggregates cited in the README. It is a public methods note, not a claim that the underlying private logs are reproducible from this repository.

## One-task harness comparison

Each harness ran `pylint-dev__pylint-7114` once using the same local model: Qwen3.8-Flash-Next, 6-bit, on oMLX. Claude Code and Codex were each pointed at that locally served model through their own endpoint settings, so all harnesses in the comparison ran the same model on one local server. The reported dollar figures apply Claude Sonnet 4.5 list prices to recorded token counts for a common comparison unit; they are not billed costs. Each resulting patch passed the held-out failing test and 56 existing tests.

The minutes exclude restart and slice interruptions. The first-call generation exclusion and the interruption/resume-call exclusion were applied once to every harness; source segments can still include tool waits. The result is one task and one run per harness. It cannot distinguish a repeatable harness difference from normal run-to-run variation, and it does not compare Claude Code or Codex running through CORSIA with running them alone.

| Harness | Model calls | Cost at Sonnet 4.5 prices | Minutes excluding interruptions | Patch review score (of 5) |
|---|--:|--:|--:|--:|
| Claude Code | 91 | $4.75 | 42.1 | 4.5 |
| Codex | 64 | $3.30 | 38.5 | 4.5 |
| CORSIA, built-in agent loop | 53 | $3.06 | 48.7 | 4.6 |

## Operational aggregates

The lease and command figures are historical aggregates for the week ending 2026-10-02 on the install that builds CORSIA. They describe counts, not a general reliability rate or a guarantee for other installs.

- 519 lease requests had a review outcome: 471 (91%) automatically approved, 34 rejected, and 14 sent to a person.
- 53 requests failed before a review outcome and are excluded from that denominator.
- 1,586 commands under leases were reviewed: 1,538 allowed and 48 blocked.

A separately sourced weekly ledger sample recorded 27.65 billion tokens. It is a limited time-bounded sample, not a lifetime total.

## Merged pull requests

Work pull requests are counted once each when merged into the development branch or into a team's integration branch (`train/*`). Merges of an integration branch into the development branch, and promotions of the development branch to the release branch, are excluded. Counts include the project's earlier repository as well as the current one. Dates are merge times in Pacific time; weeks start on Monday. The 30-day figure covers 2026-09-03 through 2026-10-02, from data exported on 2026-10-02. The count shows throughput, not quality.

## Review and quality

The two figures come from different windows and repositories, so they are not combined.

**First push.** These cover the 384 pull requests merged into the development branch from 2026-10-01 21:47 UTC to 2026-10-10 01:05 UTC. A merge of an integration branch counts as one pull request. Items merged into an integration branch are not counted individually, because they get no per-item CI or review. "First push" means the earliest commit in the merged pull request's history, and 243 of those commits carried CI results. Of these 243, 78 (32%) had a failed or timed-out check or a reviewer block. CI failed on 51 (21%). The reviewer, which runs only after CI passes, blocked 27 of the 175 first commits it reviewed (15%). Force-pushed or squashed first attempts drop out of the history, so these rates are lower bounds. Eleven of the CI failures came from a phone-width layout check that has since been removed from per-pull-request CI; without it, the combined rate is 28%.

**Reviewer audit.** Nightly audits covered 14 Pacific days between 2026-09-10 and 2026-09-27, on the project's earlier repository. An independent auditor and then the quality owner re-checked each finding against the commit it reviewed. The audits covered every blocking finding on those days and a sample of lower-severity flags. 439 of 462 findings (95.0%) were correct; the daily rate ranged from 80% to 100%. Almost all of the 23 misses were over-escalations: the observation was true, but the severity was wrong. The same audits read a risk-ranked sample of merged pull requests and filed the medium- and high-severity defects that had escaped review. Escapes were found on most days with heavy volume. Because unaudited pull requests are unknown rather than clean, no escape rate is claimed.

**Anti-pattern coverage.** The reviewer receives the full anti-pattern catalogue. The blocking CI lint in `tools/antipatterns` automates only the patterns that can be detected mechanically, such as silently swallowed errors and hard-coded model names. Process and user-experience patterns are left to review.

## Lifetime estimate

Including early experimentation and sessions outside CORSIA's accounting, total traffic is roughly estimated in the hundreds of billions of tokens. This is a retrospective estimate with incomplete ledger coverage, not an audited or independently reconstructable lifetime total or a measured bill. It includes cached context read repeatedly.

Raw workspace logs and account-level details are not published. The README labels historical throughput separately and does not present it as a quality measure; review figures are reported on their own.
