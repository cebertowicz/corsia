# How CORSIA works

These pages explain how CORSIA is built and why, for an engineer deciding whether to adopt it or contribute to it. Each page says what a part is, why it exists, how it works, what can go wrong, and where it lives in the code. For a file-by-file map of the code, read [ARCHITECTURE.md](../../ARCHITECTURE.md); these pages explain the ideas behind that map.

These pages were last checked against the code at commit `43a8c7780`, and that check did not cover every claim. First-time setup was read from the code, not run on a real install. Nothing rechecks these pages automatically, so a later change can leave one out of date. Check the linked source when using them with a different version.

## Reading order

| Page | Read it to learn |
|---|---|
| [Architecture](architecture.md) | The processes, the database, and what happens at startup |
| [Scopes](scopes.md) | How a workspace is divided into projects, and what each level owns |
| [Agents and threads](agents-and-threads.md) | What an agent is, how threads relate, and how work is delegated |
| [Turns and delivery](turns-and-delivery.md) | How a message becomes a turn, and how it survives a restart |
| [Plans](plans.md) | How commitments, waits and decisions are recorded |
| [Context and memory](context-and-memory.md) | What an agent is told, in what order, and what survives a checkpoint |
| [Models and harnesses](models-and-harnesses.md) | How a model is chosen and run, and what a switch preserves |
| [Coordination models](coordination-models.md) | How a project's way of working is packaged and versioned |
| [Measuring work](measuring-work.md) | What CORSIA counts, and what it refuses to claim |

Security is covered in its own section: [Security](../security/README.md). These pages mention security only where it changes how a part works, and link there instead of repeating it.

## Terms used throughout

- **Workspace:** the top-level folder CORSIA manages. It holds shared context, shared skills, shared documents and every project.
- **Project:** a body of work inside the workspace, such as a product or a household, with its own context, skills, documents and agents.
- **Scope:** the level something belongs to: workspace, project or thread. Context and skills are resolved by scope.
- **Thread:** one conversation with its own history and model, and optionally a plan. Agent turns run in threads.
- **Agent:** an enabled persistent-agent thread, typically given a standing responsibility. A temporary worker is a thread created for a bounded task.
- **Conductor:** the thread designated to coordinate a project. The workspace coordinator is called the CORSIA agent.
- **Turn:** one run of a thread's agent loop, from an admitted input until it finishes.
- **Harness:** the program that runs the agent loop for a turn. CORSIA has three: its own native loop, Claude Code and Codex.
- **Checkpoint:** a summary that, when installed, replaces covered earlier history in the model's context.
- **Plan:** a thread's or scope's structured record of tasks, with acceptance criteria, waits and decisions.
- **Context source:** one file or generated block that is delivered to a model, such as a system rule, a project file or the skills index.
- **State root:** the folder where CORSIA keeps its database and runtime files.

## What these pages do not cover

- How to install or use CORSIA. See the project [README](../../README.md).
- Security and the sandbox model. See [Security](../security/README.md).
- The measurement page in the app. See [Measurement](../measurement.md); [Measuring work](measuring-work.md) covers how it is built.
