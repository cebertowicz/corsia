# Spawn policy

System spawn rules (classifying work, tier defaults, failure and admission guards) ship with CORSIA and always apply. This file holds this workspace's lineup: the models you can dispatch to and their limits.

> **Template.** Replace every `<…>` placeholder with this workspace's real
> lineup, and delete rows for providers you have not configured. An unfilled
> placeholder is not a dispatchable model.

## Local model lineup (<owner>, <date>)

**<local-seat-name>** is the <local model family> local seat: <variant list>
are options. Only one variant is loaded at a time. A display alias such as
`<local-seat-name>` is not a dispatch identifier. Do not load, unload, download
or switch a model to satisfy a spawn.

Before every local spawn, read the current model status from this instance's
`GET /api/models`:

```bash
curl --fail --silent --show-error --max-time 10 \
  http://127.0.0.1:<port>/api/models
```

Select the permitted local variant with `backend: "network"`,
`provider: "omlx"`, and `status: "ready"`; pass its exact `litellm_model` value
as `model` (shape: `omlx@<namespace>/<model-id>`). `available` means present
but not loaded; it is not spawn-ready. Check `active_local_threads` and the
local-admission rule below before dispatch. If discovery fails, no permitted
variant is ready, or capacity is busy, use an appropriate allowed cloud worker
and report the reason, or wait. Never guess an alias, quantization suffix or
server identifier. Confirm the delegated assistant actually responds.

## Allowed targets

Use the discovered exact selector for the local seat or a cloud selector below.
Model choice never supplies user authority or substitutes for verification.
Confirm every selector against this instance's `GET /api/models` before
writing it here.

Fill each placeholder with the exact `litellm_model` of a row in this instance's `GET /api/models` that reads `status: ready` and whose `resolved_identity` matches the tier; never bind a row that is not ready.

| Target | Role and use | Boundary |
|---|---|---|
| Discovered `litellm_model` of the ready local variant — **<local-seat-name>** | Local worker: substantive reasoning, implementation, debugging, review, synthesis. | Shared capacity; verify outputs. Not the sole authority for high-consequence work. |
| `<codex/…mechanical…>` | Mechanical tier: clear, repeatable, high-volume extraction, transformation, classification, straightforward tool workflows. Bounded briefs that finish well inside its checkpoint cap. | Not for ambiguity, architecture, difficult diagnosis, long implementation, or consequential judgment. |
| `<codex/…standard…>` | Standard execution-leaning all-rounder: structured execution, code, routine research, document processing, well-defined multi-step work. | Peer to the Claude standard tier; choose that when intent, writing, product judgment, or browser work dominates. |
| `<claude-code/…standard…>` | Standard intent-leaning all-rounder: planning, interpretation, research synthesis, writing, product judgment, browser or tool workflows, ordinary execution. | Task fit overrides resource tie-breakers. |
| `<claude-code/…mechanical…>` | Mechanical language tier: small bounded writing, first drafts, summaries, quick lookups, lightweight edits, independent second opinions. | Not for high ambiguity, long implementation, or consequential judgment. |
| `<host-access reviewer selector>` | Reserved host-access lease reviewer. | Not for ordinary delegation. |
| `<claude-code/…strong…>` | Complex but bounded owner: architecture, difficult diagnosis, nuanced analysis, consequential writing, bounded implementation needing sustained judgment. | Choose the large-scale tier when work becomes open-ended, multi-system, or parallel rather than merely difficult. |
| `<codex/…large-scale…>` | Large-scale execution and orchestration: open-ended, long-running, multi-system, tool-intensive, parallel, or deep-research workflows. | Not merely because a bounded task is difficult. |

## Lineup guards

- **Checkpoint windows (<owner>, <date>):** <e.g. an operator cap on context size per tier, lower for mechanical-tier models>.
- **Delegated execution tie-break:** when Codex and Claude workers are both good
  fits, prefer the one whose subscription has more remaining capacity. Use
  Claude when its strengths in intent, planning, synthesis, writing, product
  judgment, or browser interaction materially fit better.
- **Local seat:** only the owner changes the loaded variant.
