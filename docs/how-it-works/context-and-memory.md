# Context and memory

Context is what an agent is told at the start of a turn. Memory is what outlives a conversation's history. This page covers how context is assembled and ordered, what a checkpoint keeps when history grows too large, the other memory stores, and what can go wrong.

## What it is

- **Context source:** a file or generated block delivered to the model, such as the platform's System rules, a project charter, a thread's own bootloader, or the skills index.
- **Installed context:** the exact set of sources a conversation was started with, retained until its next install boundary.
- **Checkpoint:** a structured summary of a thread's earlier history. When history nears the model's context limit, the checkpoint replaces the older messages in what the model sees.
- **Continuity entries:** the checkpoint's contents, in 13 named fields such as active constraints, decisions, facts, open questions and next actions (`backend/core/continuity_types.py`). Entries in three fields (active constraints, approvals and preferences, decisions) require evidence.
- **Knowledge base:** durable notes an agent writes and searches, kept as Markdown files.
- **Message and checkpoint search:** keyword search over a thread's history and its retained checkpoints.

## Why it exists

Models have a fixed window, and conversations outgrow it. Without a deliberate mechanism, the oldest content is dropped, and the oldest content is often the person's original intent. Context is also delivered to many agents at once, so what each receives, in what order, and which version has to be inspectable, not accidental.

## How it works

### Delivery order

Static sources are delivered broadest first (`backend/core/dynamic_context_registry.py`):

1. The platform's System rules
2. Workspace context
3. Project context
4. Role context: agent, conductor template, conductor, CORSIA agent
5. The thread's own bootloader
6. The skills index
7. The roster of other agents

Each delivered source records its version, and each turn records which sources it was started with (`backend/core/guidance_manifest.py`). Recording never blocks a turn; a failure is stored as a status on the turn.

### Installed context stays fixed

The installed-context snapshot is kept until the next install boundary (`backend/core/installed_context.py`). A source edit therefore reaches a running conversation only at the next checkpoint install, model switch, scope change or conductor designation. It does not reach it because a turn started, a process restarted or the backend restarted. The effect is that the start of what the model reads stays identical between boundaries, so guidance does not change mid-conversation without a recorded boundary.

### Tools are loaded on demand

Tools marked `core` are advertised in full. Others appear in an index and are loaded with `tools_load` and called with `tool_call`, so a thread does not carry every tool's schema in every turn (`backend/tools/catalog_entry.py`).

### Checkpoints

Pressure checkpoint:

- A checkpoint starts when the tokens remaining in the window fall to a reserve. The reserve is a tenth of the window, held between 8,192 and 32,768 tokens, raised so the checkpoint itself can finish, and never more than a quarter of the window (`backend/core/fork_pressure.py`). For a 200,000-token window that is about 28,700 tokens left, roughly 86% full; for a 32,768-token window it is 8,192 left, 75% full.

Idle checkpoint:

- After 240 seconds idle, a checkpoint is taken if at least 32,768 tokens of new history have accumulated (`backend/infra/checkpoint_admission.py`). The 240 seconds sits inside a provider's prompt-cache lifetime so the summary can read the cached prefix.

At a boundary the system decides to proceed, proceed and checkpoint, wait, adopt an existing checkpoint and proceed, or fail visibly (`backend/core/fork_fit.py`). A checkpoint costs a real model call.

Checkpoints are retained. An agent can search them, or read one exactly, with `get_checkpoints` and search its message history with `search_history` (`backend/tools/query_checkpoints.py`, `backend/memory/checkpoint_search.py`).

### Knowledge base

`write_knowledge`, `search_knowledge` and `list_knowledge` read and write Markdown files with front matter under a `memory/kb` folder in the workspace. Entries sit in one of four categories: facts, entities, decisions, preferences. Search uses ripgrep when it is installed (`backend/memory/knowledge_base.py`, `backend/tools/knowledge.py`).

### Lessons

A correction can become a lesson placed where the right audience will see it: a context file, a skill, or a trial lesson with a review date. See [ARCHITECTURE.md](../../ARCHITECTURE.md) for trial lessons.

### Skills

A skill is a folder with a `SKILL.md`. The skills index in context lists them with descriptions; the agent loads one when its description matches the task. Resolution across scopes is described in [Scopes](scopes.md). Reviewed skills can also be installed from a shared library; see [The skill library](../skill-library.md).

## What can go wrong

- **A context edit seems ignored.** Until an install boundary it is, by design. Trigger one with a checkpoint, a model switch, or a scope change.
- **A checkpoint loses detail.** It is a summary. Anything a thread must never lose belongs in a continuity entry that requires evidence, a plan, a file or the knowledge base, not only in chat.
- **Checkpoints cost model calls** and, when a model switch needs one, the person must consent before it runs.
- **Small windows checkpoint sooner.** The reserve is capped at a quarter of the window, so a 32,768-token window starts one at 75% full, against about 86% for a 200,000-token window.
- **Knowledge search is text matching.** It finds words, not meanings; a differently worded note is missed.

## Where it lives

| Part | Path |
|---|---|
| Source order and registry | `backend/core/dynamic_context_registry.py` |
| Installed-context snapshot | `backend/core/installed_context.py` |
| Delivered-guidance record | `backend/core/guidance_manifest.py` |
| Checkpoint trigger and boundary | `backend/core/fork_pressure.py`, `backend/core/fork_fit.py`, `backend/infra/checkpoint_admission.py` |
| Checkpoint contents | `backend/core/continuity_types.py` |
| Checkpoint and history search | `backend/memory/checkpoint_search.py`, `backend/tools/query_checkpoints.py` |
| Knowledge base | `backend/memory/knowledge_base.py`, `backend/tools/knowledge.py` |
| Skills | `backend/core/skills_registry.py`, `backend/core/skill_resolution.py` |
