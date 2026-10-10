# Starter kit

Generalized, public-safe workspace templates for a new CORSIA workspace. The
install steps and the source that discovers each directory are in
[`../OFFLINE-BOOTSTRAP.md`](../OFFLINE-BOOTSTRAP.md) §6.

**The System tier ships inside CORSIA, not in this kit.** The always-loaded
context (intent and authority, context and work state, verification,
human-agent interface, host-access boundary, CORSIA API access, spawn rules),
the agent and conductor role contracts, and the core skills live in
[`backend/prompts/system/v1/`](../../../../backend/prompts/system/v1/), are delivered to every thread, and
are read-only from the workspace. A workspace, project or thread may add to or
narrow System rules, never loosen them; removing or skipping a System requirement counts as loosening. Do not copy System files into the
workspace: they would be duplicates.

| Path | Install to | Contents |
|---|---|---|
| `context/SPAWN-POLICY.md` | `<workspace>/context/` | Spawn-policy lineup (**template**: fill the `<…>` placeholders) |
| `shared/standards/` | `<workspace>/shared/standards/` | The `RESPONSIBILITY.md` standard, the workspace organization standard, the development doctrine, and the reviewer's standards: engineering & architecture, product, UX/IA, and the LLM anti-pattern catalog |
| `skills/` | `<workspace>/skills/` | Workspace skills (list below) |
| `project-skills/` | `<workspace>/projects/<contrib project>/skills/` | Skills for working on the CORSIA repository: `sdlc-process` and `ci-gates-locally` (contributor editions), `corsia-test-environment` |
| `templates/PROJECT.md` | `<project>/context/PROJECT.md` (the conductor fills it in) | Charter for the contributor project |
| `templates/RESPONSIBILITY.md` | the contributor agent's `.threads/<id>/context/RESPONSIBILITY.md` | Contributor agent bootloader |

## Workspace skills

| Skill | Use |
|---|---|
| `agent-browser` | Browser automation CLI |
| `code-cleanup-audit` | Codebase cruft and shape-vs-function audit |
| `consolidate-finding` | Planning a duplication consolidation |
| `design-interrogation` | Stress-testing expensive-to-reverse designs |
| `incident-responder` | Incident checklists |
| `performance-analyzer` | Performance analysis |
| `refactoring-planner` | Phased refactoring plans |
| `security-review` | Security review |
| `test-strategy` | Test strategy |

The core skills (`agent-experience`, `agent-team-decomposition`,
`backlog-intake`, `conductor-bootstrap`, `context-stewardship`,
`decide-or-raise`, `decision-brief`, `engineering-standards`, `github`,
`memory-placement`, `prioritization`, `product-thinking`, `skill-creator`,
`task-discipline`) ship in the System tier. The kit excludes skills tied to the
maintainers' own accounts, devices, or services.
