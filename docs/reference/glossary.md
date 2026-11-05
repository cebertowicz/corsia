# Glossary

Terms used in the CORSIA documentation:

- **Agent:** an enabled persistent-agent thread, typically given a standing responsibility. See [Agents and threads](../how-it-works/agents-and-threads.md).
- **Artifact:** a file or external object registered with CORSIA, with an identity and an owner or other thread relationships.
- **Blueprint:** a versioned pack of files that can provide a starting point for a project's coordination model. See [Coordination models](../how-it-works/coordination-models.md).
- **Bootloader:** context files attached to a thread or scope and delivered to the agent as part of its starting context. See [Context and memory](../how-it-works/context-and-memory.md).
- **Checkpoint:** a summary that, when installed, replaces covered earlier history in the model's context. See [Context and memory](../how-it-works/context-and-memory.md).
- **Conductor:** the thread designated to coordinate a project. The workspace coordinator is called the CORSIA agent. See [Agents and threads](../how-it-works/agents-and-threads.md).
- **Connected app:** a connection that makes another tool or service's actions available to agents. See [Connected apps](../how-it-works/connected-apps.md).
- **Context source:** one file or generated block that is delivered to a model, such as a system rule, a project file or the skills index. See [Context and memory](../how-it-works/context-and-memory.md).
- **Coordination model:** the written operating contract for how a project's agents work together. See [Coordination models](../how-it-works/coordination-models.md).
- **Decision:** a question recorded on a plan for a person to answer, with options and a recommendation. See [Plans](../how-it-works/plans.md).
- **Delegate:** to hand a bounded task to a child thread, which can return its work and evidence to the parent. See [Agents and threads](../how-it-works/agents-and-threads.md).
- **Document:** a file that can be opened and edited in CORSIA's document editor.
- **Harness:** the program that runs the agent loop for a turn. CORSIA has three: its own native loop, Claude Code and Codex. See [Models and harnesses](../how-it-works/models-and-harnesses.md).
- **Local model:** a model served by a local or network model server rather than a cloud inference provider. See [Models and harnesses](../how-it-works/models-and-harnesses.md).
- **Pack:** a versioned bundle of related files in the shared library; a blueprint pack can carry a coordination model. See [The skill library](../skill-library.md).
- **Plan:** a thread's or scope's structured record of tasks, with acceptance criteria, waits and decisions. See [Plans](../how-it-works/plans.md).
- **Project:** a body of work inside the workspace, such as a product or a household, with its own context, skills, documents and agents. See [Scopes](../how-it-works/scopes.md).
- **Provider:** a service or server that supplies model inference. See [Models and harnesses](../how-it-works/models-and-harnesses.md).
- **Responsibility file:** an attached instruction file describing an agent's area of work, authority and other standing context. See [Agents and threads](../how-it-works/agents-and-threads.md).
- **Roster:** the set of agents in a scope and their recorded responsibilities and other details. See [Agents and threads](../how-it-works/agents-and-threads.md).
- **Scope:** the level something belongs to: workspace, project or thread. Context and skills are resolved by scope. See [Scopes](../how-it-works/scopes.md).
- **Skill:** a folder containing a `SKILL.md` that gives an agent instructions for a kind of work. See [Context and memory](../how-it-works/context-and-memory.md).
- **State root:** the folder where CORSIA keeps its database and runtime files. See [Architecture](../how-it-works/architecture.md).
- **Thread:** one conversation with its own history and model, and optionally a plan. Agent turns run in threads. See [Agents and threads](../how-it-works/agents-and-threads.md).
- **Turn:** one run of a thread's agent loop, from an admitted input until it finishes. See [Turns and delivery](../how-it-works/turns-and-delivery.md).
- **Watch:** a recorded wait for a deadline or expected event, linked to work that should resume or be checked. See [Plans](../how-it-works/plans.md).
- **Worker:** a temporary child thread created to do a bounded task for a parent. See [Agents and threads](../how-it-works/agents-and-threads.md).
- **Workflow:** a definition for a repeatable kind of work that can create plan steps for a run. See [Workflows](../how-it-works/workflows.md).
- **Workspace:** the top-level folder CORSIA manages. It holds shared context, shared skills, shared documents and every project. See [Scopes](../how-it-works/scopes.md).
