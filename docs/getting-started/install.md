# Install CORSIA

How to put CORSIA on your computer, what it needs, and the commands you use afterwards. When the installer finishes, CORSIA is running and open in your browser; the next page, [Your first project](first-project.md), starts there.

## Know before you start

- **A Mac with Apple Silicon (M1 or later) and macOS 15 or later**, or a Linux server running Ubuntu 24.04 or later or Debian 12 or later, on x86_64 or arm64. Agent Safehouse, which sandboxes agent shell commands on a Mac, is built on macOS `sandbox-exec`. Linux has no local model and runs protected commands with bubblewrap; see [Install on Linux](#install-on-linux).
- **Macs with less than 32 GB of memory need a cloud model provider for agent work.** Connect Claude Code, Codex or an API provider. The corrections classifier still runs locally.
- **Corrections are recorded once the local corrections classifier is installed.** Setup starts that download in the background and never waits for it. Corrections you make before it finishes are recorded once it is installed.
- **Agents run as your macOS user, with unrestricted network access.** Read [Security](../security/README.md) first.
- **Keep the workspace out of Documents, Desktop, Downloads and iCloud Drive.** CORSIA's sandbox refuses those folders, and macOS privacy prompts can keep interrupting background agents there. The default, `~/corsia`, avoids both; agents still reach files there through a reviewed request (see [Security](../security/README.md)).
- **Teams use a lot of tokens.** Start with one agent, watch its usage, and set spending limits on any API key.
- **Pre-release.** Releases from v0.4.1 on attach a Mac disk image and a `SHA256SUMS` file, and the installed app updates itself. The release workflow builds an unsigned app and signing is a separate manual step, so check a download before opening it ([Supply chain](../security/supply-chain.md#check-it-yourself)), or build the app from your checkout ([below](#desktop-app-from-your-checkout)).

## Install

**Desktop app:** download `CORSIA-<version>-aarch64.dmg` from the [latest release](https://github.com/cebertowicz/corsia/releases/latest), open it and copy CORSIA to Applications. The rest of this section installs from a source checkout instead.

For the source install, open Terminal and run:

```bash
git clone --branch main https://github.com/cebertowicz/corsia.git ~/.corsia
cd ~/.corsia
bash scripts/install.sh
```

The installer adds any missing prerequisites, builds the environment, creates your workspace at `~/corsia` and opens CORSIA at `http://localhost:1956`. On a Mac it asks no setup questions in Terminal; the one prompt there is your administrator password, and only if it has to install Homebrew. The rest happens in the browser:

1. Create a local account.
2. Connect a model source. CORSIA asks which subscription you pay for (Claude, ChatGPT, both or neither) and starts installing Claude Code or Codex for each one you pick. The next screen gives each tool its own **Sign in**, takes an API key, or connects a local [oMLX](https://github.com/jundot/omlx) server, and offers a fix for anything missing. A tool you are already signed in to in a terminal still needs CORSIA's sign-in.
3. CORSIA walks you through the rest of setup in its plan: more model providers, web search, GitHub, remote access, and your first project.

[Your first project](first-project.md) goes through those screens one at a time.

Running the installer again is safe. It never pulls new code: it prints the installed version, starts CORSIA only if it is not running, and points you at **Settings → System** for updates.

Updates arrive through **Settings → System → Update now**. Update lets running work finish before restarting, but goes ahead if it doesn't finish in time. If the update fails before CORSIA stops, the old version keeps running. If CORSIA has stopped and the new version then fails to start, CORSIA can be left stopped. If that happens, run `corsia start`; it starts the new version again, so if it fails again, `corsia logs` shows why. Update follows the upstream of the branch you have checked out. It refuses while the checkout has uncommitted edits (commit or stash them first) and offers nothing while you have local commits ahead of that upstream. A branch with no upstream, or one that has diverged from it, gets a message naming the branch and how to fix it.

**On your phone:** reach your Mac over a private HTTPS address, for example `tailscale serve --bg http://127.0.0.1:1956`, then use **Add to Home Screen**. [Remote access](../how-it-works/remote-access.md) describes the paths and what protects them.

## Install on Linux

The same three lines install the server and web app on Ubuntu 24.04 or later and Debian 12 or later. The Linux installer differs from the Mac one in these ways:

- It asks a few questions in Terminal, so it needs one. The first run of `corsia start` asks where your work should live, whether CORSIA runs on this computer only or as an always-on server, and for the account you will sign in with.
- It asks for administrator access (`sudo`) to install `apt` packages and the managed browser and to enable services at boot (`loginctl enable-linger`), and says so before each.
- There is no local model. Connect Claude Code, Codex or an API provider in the app.
- Protected commands run in bubblewrap. [Protected commands on Linux](../linux-sandbox.md) covers setup and limits.

## Manual install and requirements

| Need | Install |
|---|---|
| Python 3.11 (SQLite 3.45+) | `brew install python@3.11` |
| Node.js 20.19+ or 22.12+ | `brew install node` |
| Git 2.36+ | `xcode-select --install` |
| Agent Safehouse 0.11.1+, on `PATH` as `safehouse` | `brew install eugene1g/safehouse/agent-safehouse` |
| GitHub CLI, signed in with `gh auth login` | `brew install gh` |
| `agent-browser` 0.31.x (the installer adds it) | `npm install -g agent-browser@0.31 && agent-browser install` |
| Optional: web search | A Brave Search API key (entered in **Settings → Keys**) |

```bash
git clone --branch main https://github.com/cebertowicz/corsia.git ~/.corsia
cd ~/.corsia
python3.11 -m venv .venv
.venv/bin/python -m pip install -r backend/requirements.lock
(cd frontend && npm ci && npm run build)
cp .env.example .env                  # optional
scripts/corsia start
```

First-run setup runs only from a checkout at `~/.corsia`. Without the installer, the first `scripts/corsia start` runs it in Terminal: it asks where your work should live (press Enter for `~/corsia`), asks whether CORSIA runs on this computer only or as an always-on server, and asks you to create the account you will sign in with. Then open `http://localhost:1956`.

## Desktop app from your checkout

```bash
cd ~/.corsia
bash desktop/scripts/build-macos-bundle.sh --skip-dmg
```

The build downloads its own Rust, Python and resources, and needs room for the build plus a backup of your data. It leaves `CORSIA.app` in `~/.corsia/desktop/src-tauri/target/aarch64-apple-darwin/release/bundle/macos/`; copy it to Applications and open it. It offers to **Take over** your source install, or **Start fresh instead**, which stops the source install and moves it into a dated backup folder without deleting anything. Take over works like this: the app keeps your account, workspace and data where they are, stops the source install, and from then on serves them with the code it was built from. `http://localhost:1956` stops answering; use the app. Bookmarks and anything else pointed at that address, including the phone setup above, stop working with it. Take over is one-way: afterwards `scripts/corsia start` in your checkout refuses, saying the CORSIA app owns this install, and any `corsia` link in `~/.local/bin`, `/opt/homebrew/bin` or `/usr/local/bin` that pointed at your checkout's `scripts/corsia` now points at the app's command. Start and restart CORSIA from the app; there is no supported way back to running the checkout. The app then looks for updates in CORSIA's published releases, not in your checkout. Opening a newer build over an installed one is untested. If a takeover fails, your source install is restarted as it was. [The desktop app](../how-it-works/desktop-app.md) covers the app's first run, updates and where it keeps its state.

## Everyday commands

For the source install, `corsia` means `~/.corsia/scripts/corsia`. The installer links it as `corsia` in Homebrew's `bin` folder on a Mac (`~/.local/bin` on Linux); if it says to, open a new Terminal window first so the command is found. After a desktop app takeover, a `corsia` link in `~/.local/bin`, `/opt/homebrew/bin` or `/usr/local/bin` that pointed at the checkout's `scripts/corsia` runs the app's command instead.

| Command | Does |
|---|---|
| `corsia start` / `stop` / `restart` / `status` / `logs` | Run and inspect the backend |
| `corsia sources` / `sources recheck` | Show each model source with a fix for anything missing |
| `corsia setup local` / `setup server` | Switch between this-computer-only and an always-on server |
| `corsia service install` / `uninstall` | Run as a launchd service |
| `corsia user …` / `corsia token …` | Manage accounts and API tokens |
| `corsia delegate …` / `doc …` / `context …` / `providers …` | Drive threads, documents, context files and model endpoints from the shell |

A second checkout on the same machine needs its own `CORSIA_WORKSPACE`, `CORSIA_STATE_DIR` and `BACKEND_PORT`.

Checked against the code at `b23cc5ab9`.
