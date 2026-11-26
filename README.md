# C.O.R.S.I.A.

### A team of AI agents that organizes itself, checks its own work, and measures what it delivers per hour of your attention.

CORSIA is a free, open-source app that runs on your own computer. The agents run on the Claude or Codex subscription you already pay for, an API key, or a local model.

**[Download CORSIA for Mac](https://github.com/cebertowicz/corsia/releases/latest)** (Apple Silicon, macOS 15 or later): on the page that opens, download the file ending in `.dmg`. Or [run it as a Linux server](#getting-started).

![macOS, Linux server](https://img.shields.io/badge/platform-macOS_·_Linux_server-000000?logo=apple&logoColor=white)
![Claude Code](https://img.shields.io/badge/Claude_Code-supported-D97757?logo=claude&logoColor=white)
![Codex CLI](https://img.shields.io/badge/Codex_CLI-supported-10a37f)
![Local models on Apple Silicon](https://img.shields.io/badge/local_models-Apple_Silicon-555555?logo=apple&logoColor=white)
![Self-hosted: plain files and SQLite](https://img.shields.io/badge/self--hosted-plain_files_+_SQLite-2ea44f?logo=sqlite&logoColor=white)
![Built by its own agents](https://img.shields.io/badge/built_by-its_own_agents-7c3aed)
![License: AGPL-3.0](https://img.shields.io/badge/license-AGPL--3.0-blue)

[![The Activity screen in the desktop app: the person works down a ranked list of tasks one at a time, asking three agents to review a page, accepting a suggested edit, sending an email, commenting on a design, picking a flight and marking a task done; then reads a reply on the Notifications tab and opens the Measurement screen](docs/readme/hero.gif)](docs/readme/measurement-dashboard.png)

## The problem

One AI agent is easy to run. A team of agents can take on far more of your work, but a team brings two jobs that grow with every agent you add: organizing the agents, and supervising their work. Without help, both land on you.

## What CORSIA does

Each agent works toward a goal you can measure, such as how much of your time a kind of work takes, bugs that reach users, or new users who quit during setup. CORSIA drafts those goals with you, and you decide what each agent may do without asking you. Three things then work together:

**The agents organize the team themselves.** Once you connect a model and describe your work, CORSIA, the assistant you talk to first, creates the projects and agents your work needs, writes their instructions and keeps them current, and connects your tools, such as email, calendar, files and GitHub. Setup explains which instruction files are yours to edit and which the agents maintain. Each agent keeps a written plan, so its work survives a closed chat, a restart or a switch to another model.

**Every piece of work has one accountable agent, and agents check each other.** Agents message each other and hand smaller tasks to helper agents, but one agent answers for each result. When two goals pull in different directions, such as shipping fast and shipping without bugs, separate agents hold each one and review each other's work.

![On a phone, the plan shows its steps and one task's time, tokens and cost. On the desktop, the person asks three agents to review an email draft, accepts a suggested edit, reads their comments and sends the email with Send now](docs/readme/story-review.gif)

**You steer from one ranked list, and changes have to prove themselves.** You set the plans and priorities. The Activity screen lists what is waiting on you in one list, ranked by your agents against the priorities you set, each item with a one-line reason; it works on your Mac, and in your phone's browser once you connect it. The agents' working detail stays folded unless you open it. You and your agents can edit the same document, and each comment goes to the agent responsible for that part.

![On a phone, ask the agent what to focus on, see what goes into its next turn and open the priorities it follows. On the desktop, draw a box on a homepage headline, tag the Website agent in a comment, and it replies](docs/readme/story-image-comments.gif)

For every agent, CORSIA records the minutes you spent in the app on each finished result, the share of results accepted without being sent back, and what they cost; the numbers fill in as work is finished. When you correct an agent, the lesson can be tried on some of its later tasks and compared with tasks done without it before it becomes part of the agent's permanent instructions. Agents can also install reviewed workflows and skills from a shared library.

The three parts depend on each other. Agents that keep the team organized and their plans current give each result one accountable agent to measure; checks between agents with opposing goals make those numbers harder to game; and one ranked list keeps a growing team's work reviewable by you.

**Start with one agent, for any kind of work.** One agent on one piece of ongoing work is enough to begin: a household's paperwork, a client practice, a research project or a codebase. Add agents as the work grows.

[How it works](docs/how-it-works/README.md) · [What it measures](docs/measurement.md) · [How lessons are tested](docs/how-it-works/learning.md) · [All documentation](docs/README.md)

## Built by its own agents

CORSIA is built and maintained by agents running on CORSIA. They write the issues, pull requests and reviews, merge their own work after CI and a review by a separate agent pass, and prepare each release. They merged 1,069 pull requests in the 30 days to November 22, 2026. The count shows how much they ship, not how good it is ([how this is counted, and what the reviews catch](docs/readme-evidence.md)).

```mermaid
xychart-beta
    title "Merged pull requests per week"
    x-axis ["Aug 3", "Aug 10", "Aug 17", "Aug 24", "Aug 31", "Sep 7", "Sep 14", "Sep 21", "Sep 28*", "Oct 12", "Oct 19", "Oct 26", "Nov 2", "Nov 9", "Nov 16"]
    y-axis "Pull requests" 0 --> 450
    bar [31, 59, 29, 56, 96, 125, 135, 236, 405, 302, 290, 364, 287, 265, 134]
```

Weeks run Monday to Sunday, Pacific time. The September 28 week counts Monday to Friday only. From late October the count falls as the project's owner spent more of their time on other projects.

## Runs on the models you already have

Agents run on a Claude Code or Codex subscription, an API key billed by use, or a local model at no cost per use on a Mac with 32 GB of memory or more. Every model gets the same instructions, skills and tools. Changing an agent's model or account keeps its plan and documents, and when an account reaches its usage limit, the agent can move to another account you allow. CORSIA itself is free; model use is billed by whoever provides the model: your subscription's usage limits, your API bill, or nothing per use with a local model.

A team of agents uses far more of a subscription than one agent does, so start with one. For scale: over one week in November 2026, the install that builds CORSIA averaged about 22 agents at work in any given hour and used about 4.5 billion tokens a day, with a 97% cache hit rate. Each hour of agent work used 20% fewer tokens than in the same week a month earlier, and 12% fewer uncached tokens.

![Close-ups from the Measurement screen: the key figures, daily cost per hour of delivered work with its 7-day average, and each model's share of spend, cost per task and first-try rate](docs/readme/measurement-highlights.png)

*Close-ups from the Measurement screen.*

## Getting started

1. [Download CORSIA](https://github.com/cebertowicz/corsia/releases/latest) (`CORSIA-<version>-aarch64.dmg`), open it and drag CORSIA to Applications.
2. Open CORSIA. Setup continues in the app: create a local account, then connect a model.
3. Already use Claude Code or Codex? During setup, or any time after, CORSIA can read what they know about you, their skills and their connected apps. It shows you what it found and keeps only what you agree to.

To run it from a source checkout, or as a server on Ubuntu 24.04+ or Debian 12+:

```bash
git clone --branch main https://github.com/cebertowicz/corsia.git ~/.corsia
cd ~/.corsia
bash scripts/install.sh
```

The installer opens CORSIA at `http://localhost:1956`. More in the [getting-started guide](docs/getting-started/README.md).

## Safety

Agents act as your own user account, with network access, so they can work without stopping to ask at every step. What limits a mistake:

- **Sandboxed by default.** Shell commands run inside an open-source sandbox (Agent Safehouse on a Mac, bubblewrap on Linux). Reaching outside it takes a short-lived lease that a reviewer model checks; as a model, it can miss things. Leases work on macOS; on Linux, agents stay inside the sandbox.
- **Only the accounts you connect.** An agent reaches your email, calendar or other apps only after you sign in to connect them. Its instructions tell it to ask you before sending, publishing, deleting or paying on your behalf; that is an instruction, not a hard block.
- **Your workspace stays on your machine,** in plain files and a SQLite database. What an agent sends to a model goes to the provider you chose; with a local model, model calls stay on your Mac. Everything that leaves your machine, item by item, and the **Local only** setting that keeps a chat or project on local models: [data and privacy](docs/security/data-and-privacy.md).

How each control works and every known limit: [security documentation](docs/security/README.md).

## Contributing

Read [`CONTRIBUTING.md`](CONTRIBUTING.md). Open pull requests to `dev` from feature branches.

## License

CORSIA is licensed under the [GNU Affero General Public License, version 3](LICENSE) (AGPL-3.0-only). Contributions are accepted under the [Contributor License Agreement](CLA.md). Parts bundled with the app keep their own licences.
