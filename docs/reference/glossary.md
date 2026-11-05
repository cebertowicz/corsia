# Glossary

Terms used in the CORSIA documentation. Where a linked page defines a term, the entry uses that page's words, and a change to the definition goes on that page first:

- **Agent:** a thread whose agent setting is enabled. An agent is meant to outlive any one task and own an area of work. See [Agents and threads](../how-it-works/agents-and-threads.md).
- **Artifact:** a file or external object registered with CORSIA, with an identity and an owner or other thread relationships.
- **Blueprint:** a versioned pack of files that can provide a starting point for a project's coordination model. See [Coordination models](../how-it-works/coordination-models.md).
- **Bootloader:** context files attached to a thread or scope and delivered to the agent as part of its starting context. See [Context and memory](../how-it-works/context-and-memory.md).
- **Checkpoint:** a structured summary of a thread's earlier history. An installed checkpoint replaces covered older messages in what the model sees. See [Context and memory](../how-it-works/context-and-memory.md).
- **Conductor:** the designated coordinator of a project. The workspace coordinator is the CORSIA agent. See [Agents and threads](../how-it-works/agents-and-threads.md).
- **Connected app:** a connection that makes another tool or service's actions available to agents. See [Connected apps](../how-it-works/connected-apps.md).
- **Context source:** a file or generated block delivered to the model, such as the platform's System rules, a project charter, a thread's own bootloader, or the skills index. See [Context and memory](../how-it-works/context-and-memory.md).
- **Coordination model:** the files that describe a project's operating contract. See [Coordination models](../how-it-works/coordination-models.md).
- **Decision:** a question for the person, recorded on the plan with options and a recommendation. See [Plans](../how-it-works/plans.md).
- **Delegate:** to hand a bounded task to a child thread, which can return its work and evidence to the parent. See [Agents and threads](../how-it-works/agents-and-threads.md).
- **Document:** a file that can be opened and edited in CORSIA's document editor.
- **Harness:** one of `native`, `claude-code` or `codex`. It runs the loop: sends the prompt, receives tool calls, runs them, repeats until the turn ends. See [Models and harnesses](../how-it-works/models-and-harnesses.md).
- **Local model:** a model served by a local or network model server rather than a cloud inference provider. See [Models and harnesses](../how-it-works/models-and-harnesses.md).
- **Pack:** a versioned bundle of related files in the skill library; a blueprint pack carries a coordination model. The coordination model bundled in this repository is called a package. See [Coordination models](../how-it-works/coordination-models.md).
- **Plan:** a structured record owned by a thread or a scope. A given owner has at most one plan. See [Plans](../how-it-works/plans.md).
- **Project:** one body of work with its own context, skills and shared material. See [Scopes](../how-it-works/scopes.md).
- **Provider:** a service or server that supplies model inference. See [Models and harnesses](../how-it-works/models-and-harnesses.md).
- **Responsibility file:** an attached instruction file describing an agent's area of work, authority and other standing context. See [Agents and threads](../how-it-works/agents-and-threads.md).
- **Roster:** the set of agents in a scope, with each one's responsibility, paths, domains, status and notes. See [Agents and threads](../how-it-works/agents-and-threads.md).
- **Scope:** the level something belongs to: workspace, project or thread. Context and skills are resolved by scope. See [Scopes](../how-it-works/scopes.md).
- **Skill:** a folder containing a `SKILL.md` that gives an agent instructions for a kind of work. See [Context and memory](../how-it-works/context-and-memory.md).
- **State root:** the folder where CORSIA keeps its database and runtime files. See [Architecture](../how-it-works/architecture.md).
- **Thread:** a conversation with its own history, model and skills, and optionally a plan. Every turn runs in a thread. See [Agents and threads](../how-it-works/agents-and-threads.md).
- **Turn:** one execution of the thread's harness, from an admitted input until it finishes. See [Turns and delivery](../how-it-works/turns-and-delivery.md).
- **Wait:** a task set aside until a time, an event, another task or a milestone. Each wait is recorded as a watch; see [Events and automations](../how-it-works/events-and-automations.md). See [Plans](../how-it-works/plans.md).
- **Worker:** a thread created to do one bounded task for a parent thread. See [Agents and threads](../how-it-works/agents-and-threads.md).
- **Workflow:** a skill whose CORSIA data file (`corsia.yaml`) declares a list of steps. See [Workflows](../how-it-works/workflows.md).
- **Workspace:** everything shared across all of a person's work. See [Scopes](../how-it-works/scopes.md).
