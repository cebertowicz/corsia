# corsia-contrib project

<!-- Charter template for a contributor workspace's project. The conductor fills it
from the owner interview (conductor-bootstrap §2). Mark inferred lines
ASSUMED: <source>. Keep it under 900 words. No live status. -->

## Purpose

Donate part of the owner's weekly model budget to the upstream CORSIA
project by executing issues the maintainers have marked eligible, and report
setup, tool, and agent-experience gaps found while doing so.

## Objectives

| Objective | Success evidence | Review horizon |
|---|---|---|
| Merge-ready contributions | Upstream PRs merged without maintainer rework | <date> |
| Useful upstream reports | Issues and comments the maintainers act on | <date> |
| Bounded spend | Weekly spend within the grant | Weekly |

## Owner position and authority

The owner of this workspace sets the budget, the hours, and what this
workspace may do. The owner does **not** decide upstream product behavior,
design, merge, or release; those belong to the upstream maintainers. No agent
here may speak for the upstream project or for the owner beyond the
cross-workspace protocol.

## Boundaries and non-goals

- Do not build features upstream has not approved.
- Do not run private forks of upstream behavior in the serving checkout,
  apart from the local user-token edit the bootstrap requires.
- Do not move data from this workspace upstream (protocol §5).

## Standing risks

| Risk | Counterweight |
|---|---|
| Work lands without design approval and is rejected | Protocol §3 approval before any code |
| Private data leaks into an issue or PR | Protocol §5 outgoing-text check |
| Spend exceeds the donation | Standing grant cap; weekly self-review |
| Duplicate reports waste maintainer attention | Search first; comment instead |

## Canonical sources

| Question | Source |
|---|---|
| Cross-workspace rules | `docs/contributing/bootstrap/CROSS-WORKSPACE-PROTOCOL.md` in the repository |
| Install | `docs/contributing/bootstrap/OFFLINE-BOOTSTRAP.md` |
| Workspace settings and access (models, project, reviewer, webhook, phone, desktop, launchd) | `docs/contributing/bootstrap/workspace-setup.md` in the repository |
| Code architecture | `source/corsia/ARCHITECTURE.md` |
| Live work | Thread structured plans |
| Contributor agent's reference | `shared/contrib/INDEX.md` |
