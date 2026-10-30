# Coordination models

A coordination model is the written agreement about how a project's agents work together: who decides what, how work moves, what must never happen, and how success is measured. This page covers how a model is packaged, how it reaches the agents, how one is shared, and what can go wrong.

## What it is

- **Coordination model:** the files that describe a project's operating contract. They are ordinary Markdown in the project's `context/` folder.
- **Core rules and project layer:** a model has generic rules (`COORDINATION-CORE.md`) and a project-specific file (`COORDINATION-MODEL.md`). Where the project layer states an exception, it governs.
- **Package:** a versioned bundle of those files plus templates, with a manifest. The bundled `corsia-default` package is unpublished.
- **Conductor:** the thread that drafts and maintains the project's model. See [Scopes](scopes.md).

## Why it exists

Every project needs rules for ownership, approval, verification and escalation, and writing them from nothing costs the person hours of setup. The goal is that the person answers a short interview and the conductor drafts the rest, instead of hand-wiring a process. A package makes that starting point reusable and versioned, and makes it possible to say what changed between versions.

## How it works

### What `corsia-default` contains

The manifest (`docs/coordination-models/corsia-default/manifest.yaml`) lists package files with sizes and SHA-256 pins. It describes:

- A project charter template, a project-layer template and a safety-invariants template, with placeholders.
- The generic core rules.
- Additive role context for ordinary agents and for the conductor.
- A responsibility template and a standard for writing one.
- A routing policy for which model tier to use, with tier placeholders instead of model identifiers. The manifest calls for it to be filled from the live model catalog at install; this version has no install script.
- Interview questions, each naming the placeholders it fills.
- A starter roster: a conductor and at most one owner role.
- Five declared KPIs: first-try acceptance, rejections per outcome, fresh tokens per outcome, asks per outcome and active hours per outcome. Each refers to a metric in the measurement registry; targets are left to each project. See [Measuring work](measuring-work.md).

The package is version 0.1.0 and marked unpublished.

### How a model is written for a project

A new project scaffold creates `context/PROJECT.md` and `context/COORDINATION-MODEL.md` as templates (`backend/core/scaffold_layouts.py`). The System skill `conductor-bootstrap` guides a conductor through the interview and fills them in (`backend/prompts/system/v1/skills/conductor-bootstrap/SKILL.md`). The index describes the intended use: pick a fitting package, install its `context/` files, and write only what is local to the project in `context/COORDINATION-MODEL.md` (`docs/coordination-models/INDEX.md`).

### How a model reaches agents

Top-level files ending in `.md` in a project's `context/` folder are delivered as project context, in this order (`backend/core/project_context.py`):

1. `PROJECT.md`
2. Files whose names start with `VISION` or `INTENT`
3. Files whose names start with `SAFETY` or contain `INVARIANT`
4. All other files, by filename, which includes `COORDINATION-CORE.md`
5. `COORDINATION-MODEL.md`

`PROJECT.md` and `COORDINATION-MODEL.md` are required when project context is collected from disk. If either is missing, that collection fails with a named error instead of proceeding with partial policy. This order puts identity and intent before procedure, and the project-specific layer last. Role files under `context/roles/agent/` and `context/roles/conductor/` are delivered only to threads in that role (`backend/core/role_context_sources.py`). The conductor and CORSIA-agent role templates ship with CORSIA (`backend/prompts/conductor/`).

Delivery follows the rules in [Context and memory](context-and-memory.md): an edited file reaches a running conversation at its next install boundary.

### Sharing a model

The shared skill library can carry a coordination model as a **blueprint pack**. Fetching one is limited to the scope's designated coordinator (`backend/library/pack_fetch.py`). Before anything is written, the fetch checks that (`backend/library/pack_validation.py`):

- Each path is relative, has no dot-prefixed part and no `scripts` folder.
- Content is UTF-8 text; binary files are allowed only under `assets/` and only as recognized images or fonts.
- The digest of all files matches the library's index entry.
- The manifest declares schema `corsia.pack/1`, kind `blueprint`, and the indexed name and version.

A withdrawn pack is refused. A pack is published into the fetching thread's `packs/<name>/<version>/` folder by an atomic rename that never replaces an existing copy (`backend/library/pack_publish.py`). Fetching places the files; adopting them into a project is the conductor's separate work.

## What can go wrong

- **Unpublished and unproven.** `corsia-default` is version 0.1.0. Its bundled tests guard structure only: placeholder coverage, pinned sizes and digests, manifest shape (`tools/test_corsia_default_package.py`). The results table lists no behavioral evaluation of the model (`docs/coordination-models/RESULTS.md`). Treat it as a starting draft.
- **Manifest and library check differ.** The library validator requires `kind: blueprint` in a pack manifest; the in-repository `corsia-default` manifest does not declare a `kind`. Check how a pack is built for the library before assuming the repository copy can be fetched as it is.
- **A cited file is absent.** The core rules cite an optional standards file that this package version does not ship (the manifest says so).
- **Missing required files stop a fresh collection.** A project without `PROJECT.md` or `COORDINATION-MODEL.md` cannot collect project context from disk. A conversation can continue using its retained snapshot until an install boundary.
- **Rules are advisory text.** A coordination model changes what agents are told. It is not enforced by code, except where a rule is also implemented elsewhere, such as the scope checks in [Scopes](scopes.md).

## Where it lives

| Part | Path |
|---|---|
| Bundled package | `docs/coordination-models/corsia-default/` |
| Package index and results | `docs/coordination-models/INDEX.md`, `docs/coordination-models/RESULTS.md` |
| Package structure tests | `tools/test_corsia_default_package.py` |
| Project scaffold | `backend/core/scaffold_layouts.py` |
| Project context order | `backend/core/project_context.py` |
| Role context | `backend/core/role_context_sources.py`, `backend/prompts/conductor/` |
| Blueprint fetch and validation | `backend/library/pack_fetch.py`, `backend/library/pack_validation.py`, `backend/library/pack_publish.py` |
| Interview skill | `backend/prompts/system/v1/skills/conductor-bootstrap/SKILL.md` |
