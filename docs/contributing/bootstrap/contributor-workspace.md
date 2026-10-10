# Contributor workspace variant

Use this when the workspace exists to donate work to an upstream repository
whose maintainers keep product and design ownership, such as the CORSIA
repository itself. This is the shape; `workspace-setup.md` in this same
directory has the exact commands for every step below marked *(setup)*.

## Shape

- **One project** (for example `corsia-contrib`) holds the upstream
  repository as a registered, branch-pinned (`dev`), protected worker clone.
  Never put the contributor agent at the workspace root: it would lose
  checkout protection and `worktree_create`. *(setup: `workspace-setup.md` §2)*
- **One conductor** (it may be the workspace conductor) and **one
  contributor agent** in that project. Add a second agent only when recurring
  work needs distinct context. More agents cost more of the owner's
  attention and do not increase the token budget being donated.
  *(setup: `workspace-setup.md` §2)*
- **The charter** (`context/PROJECT.md`, template in
  `starter-kit/templates/PROJECT.md`) states that upstream maintainers own
  product direction, design, and merge. This workspace executes approved
  issues within a token budget the owner sets.
- **The coordination model** points at
  `docs/contributing/bootstrap/CROSS-WORKSPACE-PROTOCOL.md` for pickup
  criteria, design approval, PR conventions, and data boundaries. Do not
  restate them.
- **Project skills:** copy `starter-kit/project-skills/*` into the project's
  `skills/`.
- **Shared category:** create the project's `shared/contrib/` category with
  an `INDEX.md` (per `shared/standards/WORKSPACE-STANDARD.md`). It holds the
  contributor agent's claimed and merged issues, decision log, failure
  catalog, and budget ledger.
- **CORSIA access:** the contributor agent uses the API token the owner
  created (`OFFLINE-BOOTSTRAP.md` §4A) and follows
  `context/CORSIA-API-ACCESS.md`. Never ask the owner for a password or a
  token value.
- **Reaching the instance beyond `localhost`:** phone access over the
  tailnet, the desktop app, the launchd service, and the PR reviewer's
  webhook ingress are all optional and all set up the same way — see
  `workspace-setup.md` §3–§7.

## Budget

Ask the owner for the weekly token budget they will donate and when work may
run. Record both as a standing grant on the contributor agent's
`RESPONSIBILITY.md`. The agent tracks spend against the grant in its weekly
self-review and stops picking up work when the budget is spent.

## Coordination model sections to fill

| Section | Contributor-workspace content |
|---|---|
| Operating shape | The contributor agent executes; upstream lane owners design and decide; the owner sets the budget and approves anything that leaves the workspace beyond the protocol |
| Work intake | Standing grant: eligible upstream issues per the protocol §2, within budget. Nothing else starts without the owner |
| Change path | Protocol §3: design proposal on the issue, then maintainer approval, then implementation, then PR |
| Routing defaults | Prefer the local model and the mechanical tier for bounded steps; use a strong tier only for approved designs that need it |
| Coordination routes | Upstream issue → claim comment → plan node → worktree → PR → maintainer merge; findings and friction → upstream issues |

## What to report upstream

File or comment on upstream issues using `backlog-intake` and the protocol
§6. Always search first, and comment on the existing issue when one matches.

- **Agent-experience (AX) gaps:** tool schemas, error text, missing context,
  confusing skill triggers. Include the step in the agent's journey
  (`agent-experience`) and the model and harness.
- **Tool errors:** the exact error and the redacted argument shape.
- **Bugs:** reproduced on the current `dev` head.
- **Setup friction:** the step in `OFFLINE-BOOTSTRAP.md` that failed or was
  unclear, and what fixed it.
- **Missing infrastructure you hit:** see `system-overview.md` § Not built yet.
  Comment on the existing tracking issue with your concrete use case rather
  than building a private workaround.
