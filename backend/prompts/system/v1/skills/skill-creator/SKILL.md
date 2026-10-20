---
name: skill-creator
description: Use when creating, relocating, or updating a CORSIA skill—choose the location, keep the trigger discoverable and instructions concise, avoid shadowed or unreadable sources, and verify the skill agents will actually receive.
---

# Skill creator

Skills are small procedural memory, not documentation.

## Ground

Read the existing skill completely. Inspect related project, workspace, and System skills and the effective set an agent receives. Identify trigger examples and failure cases. Reuse the canonical skill; do not add a second copy. Check your skills index, including its names-only line; if nothing fits, search the library with `skill_library` action `search` (load it with `tools_load`).

## Location

The narrowest scope whose agents trigger the skill:

- One agent: `<project>/.threads/<agent-id>/skills/<name>/`, written by that agent.
- Several agents in a project: `<project>/skills/<name>/`.
- Across projects: `<workspace>/skills/<name>/`.
- Project and workspace skills are the conductor's to write; others propose (skill `memory-placement`).
- `.corsia/context` and `.corsia/skills` are generated System projections; the packaged System content is the source. Never edit them. A local skill with a System skill's name is shadowed and has no effect, so use a new name and non-overlapping trigger to add to System behavior.
- A wrong or missing System skill follows skill `context-stewardship` for upstream proposals and unavailable routes; do not hide a replacement in a local copy.
- Do not make a private runtime, cache, or plugin path the only readable source of a procedure agents need. Record an inaccessible advertised skill as a capability gap.

## SKILL.md

Lowercase hyphenated name equal to the folder. The description is the trigger: what the skill does, the requests or states that require it, and what it is not for, kept short. All trigger guidance goes there because the body loads only after it fires.

The body is imperative and holds only non-obvious procedure, decision rules, required tools, and verification. Prefer a rule, template, or wrong/right pair over explanation. Move large conditional detail to a one-level reference. Give each concept one home and do not restate core context. Reference another skill only if it ships, using the form `skill` followed by its backticked name, as this file does.

```yaml
---
name: example-skill
description: What it does and when it must be used.
---
```

Frontmatter is block style and holds spec fields only. CORSIA data (a non-default handler or a workflow) goes in `corsia.yaml` beside SKILL.md, named by the `metadata` key `corsia-data` (see skill `decision-brief`).

No README, changelog, or other process files.

## Update and verify

Update the canonical skill rather than appending contradictory text. Re-read it and check: frontmatter valid; trigger explicit; body consistent; referenced resources readable with ordinary tools; the intended copy wins precedence; and a fresh turn receives it when the trigger applies. If delivery or resolution cannot be inspected, state that gap instead of claiming the skill is active.
