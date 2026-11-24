# README evidence and limits

This record explains how the figures in the README are counted and what they cannot show: merged pull requests, review and quality, and token scale. It also keeps earlier dated readings that the README no longer shows: a one-task harness comparison, a sample of one agent's actions, and lease counts that the security pages cite. It is a public methods note, not a claim that the underlying private logs are reproducible from this repository.

## One-task harness comparison

Each harness ran `pylint-dev__pylint-7114` once using the same local model: Qwen3.8-Flash-Next, 6-bit, on oMLX. Claude Code and Codex were each pointed at that locally served model through their own endpoint settings, so all harnesses in the comparison ran the same model on one local server. The reported dollar figures apply Claude Sonnet 4.5 list prices to recorded token counts for a common comparison unit; they are not billed costs. Each resulting patch passed the held-out failing test and 56 existing tests.

The minutes exclude restart and slice interruptions. The first-call generation exclusion and the interruption/resume-call exclusion were applied once to every harness; source segments can still include tool waits. The result is one task and one run per harness. It cannot distinguish a repeatable harness difference from normal run-to-run variation, and it does not compare Claude Code or Codex running through CORSIA with running them alone.

| Harness | Model calls | Cost at Sonnet 4.5 prices | Minutes excluding interruptions | Patch review score (of 5) |
|---|--:|--:|--:|--:|
| Claude Code | 91 | $4.75 | 42.1 | 4.5 |
| Codex | 64 | $3.30 | 38.5 | 4.5 |
| CORSIA, built-in agent loop | 53 | $3.06 | 48.7 | 4.6 |

## Where an agent's actions go

The sample is one long-running agent on the install that builds CORSIA, over one night in November 2026 (about 7 PM to 6 AM Pacific). Ten groups of twenty records were taken, spread evenly over the night; four of the 200 were not actions (three usage-limit notices or empty records, one text note), leaving 196. An action is one tool call in a turn: running a command, reading a file, updating the plan, sending a message. The groups and the six categories were fixed before any record was read, and each action was assigned to one category by hand.

"Share of text the agent wrote" is each category's share of the characters in the actions' arguments, not counting the agent's prose in the turn. Arguments under about 480 characters were estimated by eye to the nearest 50, so this column is rougher than the action shares. Neither column is a share of tokens or dollars: every action makes the model re-read the agent's context, and actions differ in how much text they bring back.

This agent coordinates other agents and keeps an unusually large plan, so its shares say nothing about other agents or other installs. Treated as a simple random sample of 196, the largest share (77%) has a 95% margin of about 6 percentage points either way and the smaller shares 3 to 4. The actions were taken in ten groups of consecutive records, and the groups were uneven (plan actions per group ranged from 0 to 5), so the true margins are wider than these. Shares are rounded to whole percentages and may not sum to 100. No figure per finished task is given, because the sample has no task boundaries. The count will be replaced by readings computed from CORSIA's own usage records once they exist.

## Operational aggregates

The lease and command figures are historical aggregates for the week ending 2026-10-02 on the install that builds CORSIA. They describe counts, not a general reliability rate or a guarantee for other installs.

- 519 lease requests had a review outcome: 471 (91%) automatically approved, 34 rejected, and 14 sent to a person.
- 53 requests failed before a review outcome and are excluded from that denominator.
- 1,586 commands under leases were reviewed: 1,538 allowed and 48 blocked.

A separately sourced weekly ledger sample recorded 27.65 billion tokens. It is a limited time-bounded sample, not a lifetime total.

## Merged pull requests

Work pull requests are counted once each when merged into the development branch or into a team's integration branch (`train/*`). Merges of an integration branch into the development branch, and promotions of the development branch to the release branch, are excluded. Counts include the project's earlier repository as well as the current one. Dates are merge times in Pacific time; weeks run Monday to Sunday. The 30-day figure covers 2026-10-24 through 2026-11-22, counted from the repository's Git history on 2026-11-23; it starts mid-week, so it is not a sum of weekly bars. The last bar, the week of 2026-11-16, is complete. A pull request is identified by the number in its merge or squash commit; commits pushed without a pull request are not counted. Weekly bars through the week of September 28 (Monday to Friday) come from data exported from the earlier repository on 2026-10-02; bars from the week of October 12 come from this repository's history. The week of October 5 is left out because the move between repositories left no complete record of it. The count shows throughput, not quality.

## Review and quality

The two figures come from different windows and repositories, so they are not combined.

**First push.** These cover the 384 pull requests merged into the development branch from 2026-10-01 21:47 UTC to 2026-10-10 01:05 UTC. A merge of an integration branch counts as one pull request. Items merged into an integration branch are not counted individually, because they get no per-item CI or review. "First push" means the earliest commit in the merged pull request's history, and 243 of those commits carried CI results. Of these 243, 78 (32%) had a failed or timed-out check or a reviewer block. CI failed on 51 (21%). The reviewer, which runs only after CI passes, blocked 27 of the 175 first commits it reviewed (15%). Force-pushed or squashed first attempts drop out of the history, so these rates are lower bounds. Eleven of the CI failures came from a phone-width layout check that has since been removed from per-pull-request CI; without it, the combined rate is 28%.

**Reviewer audit.** Nightly audits covered 14 Pacific days between 2026-09-10 and 2026-09-27, on the project's earlier repository. An independent auditor and then the quality owner re-checked each finding against the commit it reviewed. The audits covered every blocking finding on those days and a sample of lower-severity flags. 439 of 462 findings (95.0%) were correct; the daily rate ranged from 80% to 100%. Almost all of the 23 misses were over-escalations: the observation was true, but the severity was wrong. The same audits read a risk-ranked sample of merged pull requests and filed the medium- and high-severity defects that had escaped review. Escapes were found on most days with heavy volume. Because unaudited pull requests are unknown rather than clean, no escape rate is claimed.

**Anti-pattern coverage.** The reviewer receives the full anti-pattern catalogue. The blocking CI lint in `tools/antipatterns` automates only the patterns that can be detected mechanically, such as silently swallowed errors and hard-coded model names. Process and user-experience patterns are left to review.

## Token scale

The README's scale line comes from CORSIA's own usage records on the install that builds CORSIA, for 2026-11-16 00:00 to 2026-11-23 00:00 UTC, compared with 2026-10-19 00:00 to 2026-10-26 00:00 UTC. Both windows cover every agent and chat on the install, persistent agents and temporary workers alike, in every project and on every model.

- An agent-hour is one agent in one clock hour (UTC) with at least one model call; one call counts the whole hour. "About 22 agents at work in any given hour" is the week's 3,620 agent-hours over its 168 hours (21.5).
- Tokens are every token the models read or wrote: input, output, cache writes and cache reads. The week used 31.3 billion, about 4.5 billion a day.
- The cache hit rate is cache reads over all input tokens (input, cache writes and cache reads): 97.4%.
- Uncached tokens are all tokens except cache reads.
- Per agent-hour, the November week used 8.65 million tokens against 10.84 million a month earlier (20% fewer), and 258,000 uncached tokens against 293,000 (12% fewer).

The two weeks had about the same number of agent-hours (3,620 and 3,766), but different work, agents, models and accounts. The comparison does not separate the effect of changes to CORSIA from those differences. An agent-hour counts the whole hour however little of it the agent spent working, so tokens per agent-hour is not tokens per hour of continuous work.

## Lifetime estimate

Including early experimentation and sessions outside CORSIA's accounting, total traffic is roughly estimated in the hundreds of billions of tokens. This is a retrospective estimate with incomplete ledger coverage, not an audited or independently reconstructable lifetime total or a measured bill. It includes cached context read repeatedly.

Raw workspace logs and account-level details are not published. The README labels historical throughput separately and does not present it as a quality measure; review figures are reported on their own.
