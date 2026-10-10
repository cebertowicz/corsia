# Contributor bootstrap pack

**Status:** draft · **Owner:** CORSIA maintainers · **Review:** every release

This pack lets an outside contributor run their **own, fully separate**
CORSIA instance and workspace and donate agent work to this repository. No
onboarding feature is involved: a Claude Code or Codex session on the
contributor's machine follows these documents by hand.

## Reading order

The platform sets itself up: the offline session does only the minimum to
get you to a running conductor thread; that conductor does everything after
that, interviewing you as it goes.

| # | Document | For | What it does |
|---|---|---|---|
| 1 | [`OFFLINE-BOOTSTRAP.md`](OFFLINE-BOOTSTRAP.md) | The offline Claude Code or Codex session | Install, sign-in and agent token, workspace layout, one working provider, load the starter kit, create and hand off to a conductor thread |
| 2 | [`starter-kit/`](starter-kit/README.md) | Copied into the new workspace | Workspace templates (spawn-policy lineup), standards, the non-core global skills, project skills, templates. The System context, role contracts and core skills ship inside CORSIA (`backend/prompts/system/v1/`), not in the kit |
| 3 | [`System conductor-bootstrap skill`](../../../backend/prompts/system/v1/skills/conductor-bootstrap/SKILL.md) | The new workspace's conductor | System overview, conductor setup, coordination model, what is not built yet, what to report |
| 4 | [`contributor workspace reference`](contributor-workspace.md) | The conductor, for a contributor workspace | Project/roster shape, budget, what to report upstream |
| 5 | [`workspace setup reference`](workspace-setup.md) | The conductor, for a contributor workspace | Model settings, project and contributor-agent creation, phone and desktop access, the launchd service, the PR reviewer, the webhook, smoke test |
| 6 | [`CROSS-WORKSPACE-PROTOCOL.md`](CROSS-WORKSPACE-PROTOCOL.md) | The contributor's agents | What to pick up, design approval, PR conventions, data boundaries, issue hygiene |

## Principles

- **Two separate workspaces.** Only the GitHub repository is shared.
- **Design stays upstream.** Maintainers and their lane owners approve designs
  and merge; contributor agents execute approved work.
- **Every setting cites its source file.** `TODO` marks a value the
  maintainers have not verified. Never fill one in by guessing.

## Maintaining this pack

When a cited file, route, or setting changes, update the document that cites
it in the same PR. The generalized context files and skills are copies of
the maintainers' workspace sources with names, private paths, and internal
references removed. Refresh them at each release, and keep them free of
personal data. The reviewer's standards in
`starter-kit/shared/standards/` (engineering & architecture, product,
UX/IA, and the anti-pattern catalog) are also generalized copies of
workspace sources — refresh them the same way whenever those sources
change.
