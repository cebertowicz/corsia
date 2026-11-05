# How CORSIA works

These pages explain how CORSIA is built and why, for an engineer deciding whether to adopt it or contribute to it. Each page says what a part is, why it exists, how it works, what can go wrong, and where it lives in the code. For a file-by-file map of the code, read [ARCHITECTURE.md](../../ARCHITECTURE.md); these pages explain the ideas behind that map.

These pages were last checked against the code at commit `43a8c7780`. A page that names its own checked commit at its end was checked at that commit instead. A checked commit means the page was read against that code; it does not promise that every claim was checked. First-time setup was read from the code, not run on a real install. On each pull request, CI checks only that the files and links a page cites still exist, not that what the page says is still true, so a later change can leave a page out of date. Check the linked source when using them with a different version.

## Reading order

| Page | Read it to learn |
|---|---|
| [Architecture](architecture.md) | The processes, the database, and what happens at startup |
| [Scopes](scopes.md) | How a workspace is divided into projects, and what each level owns |
| [Agents and threads](agents-and-threads.md) | What an agent is, how threads relate, and how work is delegated |
| [Turns and delivery](turns-and-delivery.md) | How a message becomes a turn, and how it survives a restart |
| [Events and automations](events-and-automations.md) | How events reach owners and agents schedule work |
| [Plans](plans.md) | How commitments, waits and decisions are recorded |
| [Workflows](workflows.md) | How a repeated kind of work becomes the same plan steps every run |
| [Context and memory](context-and-memory.md) | What an agent is told, in what order, and what survives a checkpoint |
| [Documents and artifacts](documents-and-artifacts.md) | How files are registered and owned, how live documents are saved, and how comments reach agents |
| [Models and harnesses](models-and-harnesses.md) | How a model is chosen and run, and what a switch preserves |
| [Connected apps](connected-apps.md) | How tools from other services reach agents, and how outside events wake them |
| [Coordination models](coordination-models.md) | How a project's way of working is packaged and versioned |
| [Measuring work](measuring-work.md) | What CORSIA counts, and what it refuses to claim |
| [Learning from corrections](learning.md) | How corrections become lessons and how trials are judged |
| [Desktop app](desktop-app.md) | How the macOS app installs, takes over a source install, updates and stores its state |
| [Remote access](remote-access.md) | How your other devices reach CORSIA, and what each way exposes |

Security is covered in its own section: [Security](../security/README.md). These pages mention security only where it changes how a part works, and link there instead of repeating it.

## Terms used throughout

See the [glossary](../reference/glossary.md) for terms used throughout the documentation.

## What these pages do not cover

- How to install or use CORSIA. See the project [README](../../README.md).
- Security and the sandbox model. See [Security](../security/README.md).
- The measurement page in the app. See [Measurement](../measurement.md); [Measuring work](measuring-work.md) covers how it is built.
