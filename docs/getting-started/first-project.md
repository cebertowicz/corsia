# Your first project

What you see in the browser after [installing CORSIA](install.md): the setup screens, the first chat with the CORSIA agent, and creating a project. A **project** is one body of work with its own context, skills and files, kept in its own folder in your workspace; see [Scopes](../how-it-works/scopes.md). The **CORSIA agent** is the assistant you talk to outside any project.

Control names below are the labels the app shows. This page was written from the app's source code and has not been walked through in a browser; if a screen differs from what you read, the screen is right and the page needs a fix.

## Setup screens

CORSIA opens at `http://localhost:1956` on a setup screen that shows four steps in a side list: **Getting started**, **Account & workspace**, **Your subscriptions** and **Providers & models**. Jobs that run in the background, such as a model download, are listed under **In the background**.

1. **Welcome to CORSIA.** Press **Continue**. On a Mac that can run the local model, its download starts by itself when this screen opens. In the desktop app only, **Connect to CORSIA on another computer** is offered here too, for when CORSIA already runs elsewhere and you only want to open it from this one.
2. **Create your account.** Fill in **Full name**, **Username**, **Password** and **Confirm password**; **Email** is optional. The page says your account and password stay on this computer. The **Share usage counts** switch is your choice, and the work folder row shows where your workspace will live (the default is `~/corsia`; keep it out of Documents, Desktop, Downloads and iCloud Drive, as the [install page](install.md#know-before-you-start) explains). Press **Continue**.
3. **Your subscriptions.** The question is **Which do you pay for?**, answered with **Claude**, **ChatGPT**, **Both** or **Neither**; the screen adds that Claude Code and Codex are the apps you sign in to next. Picking Claude, ChatGPT or Both starts installing the matching tool right away. **Neither** is fine: it starts the local model's download if it isn't already running, or you add an API key on the next step.
4. **Providers & models.** The page says CORSIA needs one working model to continue setup. Its rows are **Claude Code** and **Codex** (only for what you picked, or a tool already signed in inside CORSIA), **Local model** (with **Download**, **Check again** or **Retry**, depending on its state) and **API provider** (**Add provider**). A Claude Code or Codex row has its own **Sign in**, pressed once the tool is installed; it is needed even when you are already signed in to that tool in a terminal. A vendor password or sign-in code is typed only on the vendor's own page, never into a CORSIA chat. Get one row to work; the other rows can wait. [Models and harnesses](../how-it-works/models-and-harnesses.md) explains what each source is.

**Start using CORSIA** stays disabled until at least one model works. A Mac with less than 32 GB of memory currently needs one of the cloud rows for agent work ([Know before you start](install.md#know-before-you-start)). On Linux there is no local model at all.

## The first chat and the setup plan

Pressing **Start using CORSIA** opens a chat with the CORSIA agent. Its first message is a fixed introduction, not a model reply. It says how help is organized:

- a quick question is a new chat;
- something that keeps coming back and taking your time is given to an agent, an AI that owns that job and answers for the result (the next page, [Your first agent](first-agent.md), does this);
- work that needs its own space, or several agents together, becomes a project;
- everything else stays with the CORSIA agent.

The message also points to a **plan** for finishing setup, in the **Plan** section on the right. On a phone it is the button above the message box that shows how many tasks are left. A plan is a list of tasks, each with a stated end condition; see [Plans](../how-it-works/plans.md).

The setup plan has up to nine tasks, in this order. A task that does not apply to you is skipped without a word.

| Task | What it does |
|---|---|
| Add your Claude or ChatGPT subscription | Only for a subscription you said you pay for and have not finished connecting |
| Bring over what your other tools know | Asks first, with a **Read what your other tools know?** card; only when CORSIA found another tool's data |
| Where your work lives | Asks which apps you use, such as a tracker or notes, and connects the quick ones |
| Web search | Optional Brave key, so agents can search; Claude Code's and Codex's own search is off inside CORSIA |
| About you | A few questions, written into a document you approve |
| Email and calendar | Gmail, Outlook or another host |
| How CORSIA gets better | The **Use and share improvements with the CORSIA community?** card; see [Learning](../how-it-works/learning.md) |
| Your files, and when agents ask first | A short explanation, no question; the same facts as [Security](../security/README.md) |
| Hand over to your agents | Asks what is taking your time this week, and what you would do with a free afternoon at your computer |

Every task accepts **not now**, and saying **skip the rest** closes all remaining setup tasks and goes straight to the last one. Nothing here is required to create a project. Agents ask you before they send or post as you, or spend money.

The last question is the quickest way to see what CORSIA does: describe a small real chore or side project and the CORSIA agent starts on it, or tells you which setup it would suggest.

## Create a project

You can create a project at any time, and the setup plan does not need to be finished first.

1. Open the workspace's **Activity** page (press **Workspace** in the sidebar if you are inside a project). The right pane has a **Projects** section listing your projects, or **No active projects**.
2. Press **New project** at the end of that section. The command palette's **New project** does the same.
3. In the **New project** window, type a **Name** (the placeholder is `e.g. Acme Redesign`) and, if you like, a **Description**, which is shown in the projects list, and an **Accent** colour. Names are unique: a second project with the same name is refused.
4. Press **Create project**. It reads **Creating…** while it works; **Cancel** closes the window without creating anything.

CORSIA then:

- creates a folder for the project under `projects/` in your workspace, with `context/`, `skills/`, `shared/`, `.threads/` and `source/` inside it, plus a charter and a coordination-model file in `context/`;
- creates the project's **conductor**, a chat titled `<Name> Conductor` that coordinates the project's agents and keeps its charter. It starts empty; nothing is sent to it;
- opens the project on its **Activity** page, which has nothing to list until work starts.

The conductor is the first entry in the sidebar's agent list; its tooltip reads `<Name> Conductor — main chat for this project`. A repository you already work in is linked from **Project settings** (the header's **Project settings** button) with **Add repository**. The create window has no folder field.

You can also ask the CORSIA agent to set a project up. It lists projects it finds when you allow it, creates the project and hands it to the new conductor, which asks how you work before it proposes anything. Nothing starts until you approve how the project should run.

Next: [Your first agent](first-agent.md).

Checked against the code at `b23cc5ab9`.
