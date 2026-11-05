# Your first agent

How to give a small piece of work to CORSIA inside your [first project](first-project.md), see where the result appears, and then make that chat a standing **agent**. A chat is one conversation. An **agent** is a chat with a responsibility that it keeps owning, so other chats and the conductor can address it; see [Agents and threads](../how-it-works/agents-and-threads.md).

This page needs a working model from the setup screens, because every step here sends a message to one. It was written from the app's source code and has not been walked through in a browser; if a screen differs from what you read, the screen is right and the page needs a fix.

Pick a small, harmless first task, something whose result you can check by reading it: a one-page summary of a topic, a checklist for something you do every month, or a first draft of a short document.

## Start a chat in the project

1. Open the project: press its name in the **Projects** section of the workspace **Activity** page.
2. Press **New chat** in the sidebar. Inside a project it starts the chat in that project. To start one somewhere else, hold the button and choose from **New chat in…**.
3. Type your task in the message box (**Message CORSIA**) and send it. The button on the message box shows the model the chat uses; press it to pick another. A chat on a cloud model sends what you write to that provider.

The chat has its own history, model and files. Its context also includes the project's charter and skills, because the chat belongs to the project ([Scopes](../how-it-works/scopes.md)).

## See what happened

The reply streams into the chat, with the tools the model used shown as it works. Two sections in the right pane show the rest (on a phone, the pane opens from the header, and the plan is also the button above the message box):

| Section | Shows | When empty |
|---|---|---|
| **Plan** | The chat's plan: its tasks, what each needs to count as done, and anything waiting on you. **Pause** stops the plan from continuing; only you pause or resume a plan ([Plans](../how-it-works/plans.md)) | **No plan yet**. A small task may never get one; CORSIA makes a plan for work bigger than a task or two |
| **Files** | Documents the chat created, uploaded or opened | "Files you create, upload, or open in this chat show up here." |

**Browse documents…**, under **Files**, opens the project's documents. A document the agent writes opens beside the chat, where you can read it and leave comments. The same files are plain files in the project's folder in your workspace.

If a plan task needs a decision from you, it appears on the **Activity** page under **Needs you** as a count of decisions, and as a card in the chat. Answering it is what lets the work continue.

## Make it an agent

A chat that did something useful, and that you expect to need again, can become an agent:

1. Open the chat's settings with the **Chat settings** button in the header.
2. In the **Agent** section, which says to promote the chat when it has a clear responsibility, press **Make agent**.
3. Write the **Responsibility**: the outcome this agent owns, in a sentence or two. **Paths** (folders or files) and **Topics** are optional and tell other agents what it covers.
4. Press **Save**. **Edit** and **Remove** appear in the same place afterwards.

The agent now appears in the sidebar's agent list for the project, under the conductor, with its responsibility as its tooltip. It keeps its history, plan and files, and the conductor can send it work. What it can reach is set by what [Security](../security/README.md) describes. Agents are instructed to ask you before they send or post as you, or spend money, unless you gave them standing permission; that is an instruction to the model, not a technical block.

## Or ask the conductor

You do not have to write a responsibility yourself. Open the project's conductor, the `<Name> Conductor` entry at the top of the sidebar's agent list, and tell it what is taking your time:

- The conductor looks at how you handle that work, asks you what it still needs to know, and writes a one-page charter: the workflow, the steps to automate, and the limits.
- Nothing gets authority until you approve that charter. No agent is created or enabled, and nothing is sent or spent on your behalf, before then.
- If the charter recommends a dedicated agent, the conductor creates it after your approval. If a chat or the conductor can do the job, it says so instead.

Start with one agent and watch its usage: teams use a lot of tokens ([Know before you start](install.md#know-before-you-start)).

## Where to go next

- [How CORSIA works](../how-it-works/README.md) explains the parts you have used: plans, scopes, agents, and models.
- [Security](../security/README.md) explains what agents can reach and when they ask first.

Checked against the code at `b23cc5ab9`.
