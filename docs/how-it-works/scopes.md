# Scopes

A scope is the level a piece of context, a skill or a responsibility belongs to: the whole workspace, one project, or one thread. This page covers how scopes are laid out on disk, how they resolve against each other, who coordinates each, and what can go wrong.

## What it is

- **Workspace scope:** everything shared across all of a person's work.
- **Project scope:** one body of work with its own context, skills and shared material.
- **Thread scope:** one conversation's own context, skills and scratch space.

Each scope has the same shape on disk, so an agent that understands one understands all three.

```text
workspace/
  context/      workspace context files
  skills/       workspace skills
  shared/       durable shared material
  projects/
    <project>/
      context/  project context files
      skills/   project skills
      shared/   durable shared material
      source/   the project's code, as separate repositories
      .threads/<thread>/   thread-local context, skills, scratch
  .threads/<thread>/       the same, for workspace-level threads
```

## Why it exists

Different work needs different rules. A household budget and a software product should not share a charter, a safety rule or a skill set, but both should share the platform rules above them. Scopes give a narrow default (a project's rules apply to the project) while letting broad guidance reach everything below it.

Plain folders also mean a person can read, edit and version a scope with ordinary tools, and agents edit the same files the person does.

## How it works

### Layout

The workspace scaffold creates `context`, `skills`, `shared`, `projects` and `.threads`. A project scaffold creates `context`, `skills`, `shared`, `.threads` and `source`, plus a project charter and a coordination-model file in `context/` (`backend/core/scaffold_layouts.py`). The workspace ignores `source/` and thread scratch in Git, because project source checkouts are separate repositories.

### What resolves how

- **Context** is delivered in a fixed order, broadest first: the platform's own System rules, workspace context, project context, role context, the thread's own bootloader, the skills index, then the roster of other agents (`backend/core/dynamic_context_registry.py`). Details are in [Context and memory](context-and-memory.md).
- **Skills** resolve from broad to narrow: System, workspace, project, then the thread and its ancestors. For a skill name that appears at more than one level, the narrowest wins and the broader copy is reported as overridden. A System skill name cannot be overridden: a same-named skill elsewhere is omitted as shadowed by System (`backend/core/skill_resolution.py`, `backend/core/skill_scanning.py`).

### Who coordinates a scope

Each scope can have one designated coordinator, called a conductor:

- Workspace: the CORSIA agent, stored as a setting.
- Project: the project's conductor thread, stored on the project record (`backend/infra/conductor.py`).

A conductor receives a role template on top of the shared rules (`backend/core/conductor_templates.py`). Tool-driven roster changes in a scope require a live conductor in that same scope (`backend/core/agent_roster_mutations.py`).

### Crossing scopes

Same-scope routing is the default. A message from one scope to another is refused unless the target is the workspace's CORSIA agent, sent through the server-bound delegation tool; otherwise the refusal names the target scope's coordinator and says to coordinate from there (`backend/infra/delegation_scope.py`). A thread designated conductor of one project is not an endpoint in another.

### Where the state root sits

Conversations, plans and settings are not in the workspace folders. They live in the state root described in [Architecture](architecture.md). A scope is therefore a folder plus database rows that refer to it.

## What can go wrong

- **Stale context in a running conversation.** Edits to a context source reach a running thread only at its next checkpoint install, model switch, scope change or conductor designation, not on the next message. See [Context and memory](context-and-memory.md).
- **A shadowed skill looks like a bug.** A workspace skill with the same name as a System skill is silently not used; the skills index reports it as shadowed.
- **No coordinator, no roster change.** A project without a live conductor cannot have its roster edited through tools.
- **Cross-scope requests fail closed.** The error is deliberate; route the request through the target scope's coordinator.

## Where it lives

| Part | Path |
|---|---|
| Scaffold content for workspaces and projects | `backend/core/scaffold_layouts.py`, `backend/core/workspace_scaffolding.py` |
| Designated coordinators | `backend/infra/conductor.py` |
| Conductor and CORSIA-agent role templates | `backend/core/conductor_templates.py` |
| Skill precedence | `backend/core/skill_resolution.py`, `backend/core/skill_scanning.py` |
| Cross-scope send rule | `backend/infra/delegation_scope.py` |
| Bootloader file management | `backend/core/bootloader_management.py` |
