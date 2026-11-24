# C.O.R.S.I.A.

### Most agent tools count tokens. CORSIA counts your time.

Getting an agent to do work is easy. Trusting it with enough of your work to actually save you time is the hard part, and most tools can't tell you whether it does. CORSIA measures every agent on the minutes it takes from you and how much of its work you accept on the first try, and compares the tasks it does with each new lesson against the tasks it does without.

**[Download CORSIA for Mac](https://github.com/cebertowicz/corsia/releases/latest)** (Apple Silicon, macOS 15 or later): on the page that opens, download the file ending in `.dmg`. Or [run it as a Linux server](#getting-started).

Free and open source. Agents run on what you already use: Claude Code, Codex, an API key billed by use, or a local model at no cost per use on a Mac with 32 GB of memory or more.

## What's different

**It counts your time, not just tokens.** Every agent, with no setup: your minutes per accepted result, the share of its work accepted on the first try, and cost per accepted task, each against its own past weeks. Models compete on your own work, so you can see which one earns its price.

![CORSIA measurement: hours of work per hour of your attention, first-try rate, cost per hour of delivered work, your minutes per outcome and tokens](docs/measurement-kpis-light.png)

*The Measurement screen on the install that builds CORSIA, September 25 to October 8, 2026.*

**What an agent learns is tested, not just remembered.** When an agent learns from a correction, the lesson goes on trial. Some of its tasks run with the lesson and some without; once there are enough of each, a nightly check compares them, and on the lesson's review date the agent keeps, revises or retires it. Your own instructions never expire.

**Agents set it up and keep it running.** Tell CORSIA, the assistant every install starts with, what you want off your plate, such as your inbox and follow-ups. It sets up an agent to own that, writes its instructions, and asks you only for what agents shouldn't do, like signing in to your email. When you run several agents on one piece of work, a coordinating agent changes how they work together from what the numbers show. You set what each agent may decide; a choice outside that reaches you with options and a recommendation.

**It is built by its own agents.** CORSIA's agents merged [1,069 pull requests in the 30 days to November 22, 2026](docs/readme-evidence.md), under one maintainer, on the install in the screenshot above.

[How it works](docs/how-it-works/README.md) · [What it measures](docs/measurement.md) · [How lessons are tested](docs/how-it-works/learning.md) · [All documentation](docs/README.md)

## Is it for you?

It is if:

- you want to hand more of your work to agents, and to know whether that is paying off;
- your work is email, scheduling, documents and research as much as code;
- you are willing to let agents make mistakes and learn from them.

It is not for anyone who wants to approve every step. A team of agents uses far more of a subscription than one agent does, so start with one.

## Where it stands

This way of working is proven on the install that builds CORSIA. Most of the work since has gone into adapting it to any install, which is not yet proven. 0.5.0 is the most structured release so far, and it still has gaps. What has shipped: [CHANGELOG](CHANGELOG.md).

## Getting started

1. [Download CORSIA](https://github.com/cebertowicz/corsia/releases/latest) (`CORSIA-<version>-aarch64.dmg`), open it and drag CORSIA to Applications.
2. Open CORSIA. Setup continues in the app: create a local account, then connect a model.
3. Already use Claude Code, Codex, OpenClaw or Hermes? During setup, or any time after, CORSIA can read what they know about you, their skills and their connected apps. It shows you what it found and keeps only what you agree to.

To run it from a source checkout, or as a server on Ubuntu 24.04+ or Debian 12+:

```bash
git clone --branch main https://github.com/cebertowicz/corsia.git ~/.corsia
cd ~/.corsia
bash scripts/install.sh
```

The installer opens CORSIA at `http://localhost:1956`. More in the [getting-started guide](docs/getting-started/README.md).

## Safety

Agents run as your own user account with unrestricted network access, so they can do real work without stopping to ask you at every step. CORSIA limits what a mistake can reach instead:

- **Sandboxed by default.** Shell commands run inside an open-source sandbox (Agent Safehouse on a Mac, bubblewrap on Linux), and file tools reach only allowed paths.
- **Reviewed access to anything outside.** To reach beyond the workspace, an agent asks for a short-lived lease that states its purpose. A reviewer model checks the request and every command run under it; as a model, it can miss things. Leases work on macOS only for now.
- **Only the accounts you connect.** An agent reaches your email, calendar or other apps only after you sign in to connect them. Its instructions tell it to ask you before sending, publishing, deleting or paying on your behalf; that is an instruction, not a hard block.
- **Your workspace stays on your machine,** in plain files and a SQLite database. What an agent sends to a model goes to the provider you chose; with a local model, model calls stay on your Mac.

How each control works and every known limit: [security documentation](docs/security/README.md).

## Contributing

Read [`CONTRIBUTING.md`](CONTRIBUTING.md). Open pull requests to `dev` from feature branches.

## License

CORSIA is licensed under the [GNU Affero General Public License, version 3](LICENSE) (AGPL-3.0-only). Contributions are accepted under the [Contributor License Agreement](CLA.md). Parts bundled with the app keep their own licences.
