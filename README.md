# C.O.R.S.I.A.

### AI agents that take whole jobs off your plate, and get better every time you correct them.

Tell CORSIA what you want handled: your inbox, your calendar, a codebase. It sets up agents that own that work, connects them to your tools, and brings you only the calls that are yours to make. Free and open source, running on your own machine.

**[Download CORSIA for Mac](https://github.com/cebertowicz/corsia/releases/latest)** (Apple Silicon, macOS 15 or later): on the page that opens, download the file ending in `.dmg`. Or [run it as a Linux server](#getting-started).

## How it works

| | Step | Example |
|---|---|---|
| **1** | Say what you want off your plate | "Keep on top of my inbox and chase anyone who hasn't replied." |
| **2** | Agents set themselves up | CORSIA creates an agent for that job, writes its instructions and connects your email. You only sign in. |
| **3** | Correct them as you go | Each correction becomes a lesson, tested on the agent's later work. |

## What makes it different

**Agents own jobs, not single tasks.** Each agent keeps its area of your work from one week to the next, with its own instructions, tools and history. Several agents can split one piece of work, with a coordinating agent keeping them in step.

**Corrections are tested, not just remembered.** A lesson from your correction goes on trial: some of the agent's tasks run with it and some without, and on its review date the agent keeps, revises or drops it. Instructions you write yourself never expire.

**You choose what it decides.** You set what each agent may decide on its own. Anything beyond that comes to you as one question, with options and a recommendation.

**You can see whether it's paying off.** For every agent, with no setup: minutes of your time per finished result, the share of its work you accepted on the first try, and the cost of each accepted task.

![CORSIA measurement: hours of work per hour of your attention, first-try rate, cost per hour of delivered work, your minutes per outcome and tokens](docs/measurement-kpis-light.png)

*The Measurement screen on the install that builds CORSIA, September 25 to October 8, 2026.*

CORSIA is built this way: its agents merged [1,069 pull requests in the 30 days to November 22, 2026](docs/readme-evidence.md), with one maintainer.

[How it works](docs/how-it-works/README.md) · [What it measures](docs/measurement.md) · [How lessons are tested](docs/how-it-works/learning.md) · [All documentation](docs/README.md)

## Where it stands

Proven on the install that builds CORSIA; not yet proven on anyone else's. 0.5.0 is the most structured release so far, and it still has gaps. What has shipped: [CHANGELOG](CHANGELOG.md).

## Getting started

1. [Download CORSIA](https://github.com/cebertowicz/corsia/releases/latest) (`CORSIA-<version>-aarch64.dmg`), open it and drag CORSIA to Applications.
2. Open CORSIA. Setup continues in the app: create a local account, then connect a model.
3. Already use Claude Code, Codex, OpenClaw or Hermes? During setup, or any time after, CORSIA can read what they know about you, their skills and their connected apps. It shows you what it found and keeps only what you agree to.

Agents run on what you already use: a Claude Code or Codex subscription, an API key billed by use, or a local model at no cost per use on a Mac with 32 GB of memory or more. A team of agents uses far more of a subscription than one agent does, so start with one.

To run it from a source checkout, or as a server on Ubuntu 24.04+ or Debian 12+:

```bash
git clone --branch main https://github.com/cebertowicz/corsia.git ~/.corsia
cd ~/.corsia
bash scripts/install.sh
```

The installer opens CORSIA at `http://localhost:1956`. More in the [getting-started guide](docs/getting-started/README.md).

## Safety

Agents act as your own user account, with network access, so they can work without stopping to ask at every step. What limits a mistake:

- **Sandboxed by default.** Shell commands run inside an open-source sandbox (Agent Safehouse on a Mac, bubblewrap on Linux). Reaching outside it takes a short-lived lease that a reviewer model checks; as a model, it can miss things. Leases work on macOS only for now.
- **Only the accounts you connect.** An agent reaches your email, calendar or other apps only after you sign in to connect them. Its instructions tell it to ask you before sending, publishing, deleting or paying on your behalf; that is an instruction, not a hard block.
- **Your workspace stays on your machine,** in plain files and a SQLite database. What an agent sends to a model goes to the provider you chose; with a local model, model calls stay on your Mac.

How each control works and every known limit: [security documentation](docs/security/README.md).

## Contributing

Read [`CONTRIBUTING.md`](CONTRIBUTING.md). Open pull requests to `dev` from feature branches.

## License

CORSIA is licensed under the [GNU Affero General Public License, version 3](LICENSE) (AGPL-3.0-only). Contributions are accepted under the [Contributor License Agreement](CLA.md). Parts bundled with the app keep their own licences.
