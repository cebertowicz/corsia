# Agent sandbox

How CORSIA confines an agent's shell commands and file tools, on macOS and Linux, without a lease. Leases are on [Host access](host-access.md).

**In short:** agents run as your user. File tools reach only the workspace and registered repositories. Shell commands run inside an OS sandbox that lets them read and write the workspace, read a few tool-settings folders, and reach the network freely. A project agent's shell is also fenced out of other projects (the CORSIA agent's shell is fenced less, [below](#projects)); file tools are not, so the workspace is still the boundary that holds for every tool.

## Layers

| Layer | What it covers | Strength | Source |
|---|---|---|---|
| File-tool gate | `read_file`, `write_file`, `edit_file`, `grep`, `glob`, `list_dir` | CORSIA code | `backend/tools/filesystem_gate.py`, `backend/tools/sandbox_config.py` |
| Shell sandbox | Every `bash` command and everything it starts | Operating system | `backend/tools/shell_guard.py`, `backend/tools/shell_sandbox.py` |
| Project fence | An OS rule keeping an agent's shell out of other projects' folders and registered repositories. It also covers another AI agent's command-line tool started under a lease. On macOS, local connected apps and leased shell commands are fenced only from projects set to Local models only. On Linux it covers the shell only; local connected apps [don't start](connected-apps.md#local-and-remote-connected-apps) | Operating system | `backend/tools/project_fence.py`, `backend/tools/host_path_policy.py`, `backend/tools/linux_shell.py` |
| Command refusals | A list of commands refused before they run | CORSIA code | `backend/tools/shell_guard.py` (`check_command_safety`) |

The file tools run inside the CORSIA server, so the OS sandbox doesn't apply to them; their own path check does. The shell runs in a separate sandboxed process.

## What's allowed

**Read and write:** the workspace (shared files, the agent's own project and working folder) and every registered repository, apart from what the [project fence](#projects) closes to the shell. A registered repository whose base checkout is protected keeps that main checkout read-only, with its verified worktrees under `.worktrees/` read and write; an unprotected repository is read and write throughout.

**Writable outside the workspace, from the shell (macOS):** temporary folders (`/tmp` and the system temp folder, which holds each chat's package caches), `~/.ssh/known_hosts`, and some developer-tool caches that Agent Safehouse allows, such as `~/.npm`, `~/.cargo` and `~/.rustup`. On Linux, each thread gets its own `/tmp` (below).

**Read only, from the shell:** system files such as `/etc`, and some tool settings in your home folder: `~/.gitconfig`, most of `~/.config`, most of `~/.cache`. The file tools don't reach these.

**Network:** any outbound connection, to the internet or your local network. Not limited. See [Network](network.md).

**SSH:** the shell gets your SSH agent socket (`SSH_AUTH_SOCK`), so keys loaded in your SSH agent can be used to `ssh` or `git push`. The private key files stay unreadable. `~/.ssh/config`, `known_hosts` and `allowed_signers` are readable. Once an agents' GitHub account exists, a `git push` to GitHub goes over HTTPS as that account, including one your Git settings rewrite to SSH, and a GitHub push still arriving over SSH is refused; a command that sets its own SSH command can still push with your key (`backend/connections/github_agent.py`).

## Projects

On macOS and Linux the shell sandbox fences each agent out of projects that aren't its own (`backend/tools/project_fence.py`). On Linux, a project folder or repository that doesn't exist yet isn't fenced until it exists ([Linux](#linux)). The fence is worked out again on every command, so a change to a project's Local models only setting or an agent's project applies to its next command.

| Agent | Its shell can't read or write |
|---|---|
| An agent in a project | Every other project's folder, the repositories and worktrees registered to those projects, and the working folders of agents outside any project |
| An agent outside any project | Every project's folder and registered repositories |
| The CORSIA agent | Only projects set to Local models only, and nothing while its own chat is Local only |
| A local connected app | On macOS, only projects set to Local models only; it serves no single agent. On Linux, local connected apps [don't start](connected-apps.md#local-and-remote-connected-apps) |

- A repository registered to two projects stays open to agents of both, unless one of them is set to Local models only and the agent reaches it only through the other.
- A lease opens an ordinary fenced project to that agent's leased shell commands. Another AI agent's command-line tool started under a lease stays fenced from every project but its own. A project set to Local models only stays closed under every lease.
- If CORSIA can't read a project's or repository's record, it fences more, not less.
- An agent can't register a repository that sits inside a project it is fenced from.

**What the fence doesn't cover:**

- **File tools.** `read_file`, `write_file`, `edit_file`, `grep`, `glob` and `list_dir` still reach every project in the workspace, including ones set to Local models only.
- **Chats.** An agent can read another project's chats by id ([Accounts and access](accounts-and-access.md#how-agents-act)).
- **The workspace's Git history.** A shell running `git` at the workspace root can read the committed versions of other projects' files.

If two bodies of work must stay apart, give each its own workspace and CORSIA install.

## What's refused

| Refused | Strength |
|---|---|
| The rest of your home folder: Documents, Desktop, Downloads, Pictures, Music, Movies, shell history and startup files, `~/Library` data (Mail, Messages, cookies, browser data, Keychains), `~/.ssh` private keys, `~/.aws`, `~/.docker`, `~/.kube`, `~/.gnupg`, `~/.netrc`, other AI tools' folders | Operating system |
| CORSIA's state folder (database, logs, keys, the shared browser profile, Claude Code and Codex sign-ins) and its checkout's private files | Operating system |
| Other projects' folders and registered repositories, as listed under [Projects](#projects) | Operating system |
| Writes outside the workspace and registered repositories, including your home folder and `/`, apart from the exceptions under [What's allowed](#whats-allowed) | Operating system |
| CORSIA's startup items (`LaunchAgents`), its restart control files, the generated `.corsia/context` and `.corsia/skills` folders | Operating system |
| Writes to the workspace's Git settings, hooks and pointer files (`.git/config`, `.git/config.worktree`, `.git/commondir`, `.git/hooks`) and its `.git` marker, because the server runs Git there outside the sandbox. Agents can't run `git init` at the workspace root. On Linux, the first agent command in a workspace with no `.git` makes one so the rule covers it, and a symlinked `.git` stops agent commands until it's replaced with a directory or a `gitdir:` file | Operating system for the shell, CORSIA code for file tools |
| Raw disk devices | Operating system |
| Reading or signalling processes outside the command's own sandbox (the server, other threads) | Operating system |
| The clipboard (macOS) | Operating system |
| `sudo`, `su`, `doas`, `shutdown`, `reboot`, `halt`, `poweroff`, `pmset` | CORSIA code, and the OS refuses to run those binaries |
| Piping a download into a shell (`curl … \| sh`), `eval`, process substitution | CORSIA code |
| Disk formatting and erase (`mkfs`, `diskutil erase…`), fork bombs, literal `rm -rf /` or `rm -rf ~` | CORSIA code |
| Stopping or restarting CORSIA itself | CORSIA code |
| A command that passes a literal value where a credential goes (a credential-named variable, flag, header or URL field) | CORSIA code |

The command refusals match text. A command built to avoid the pattern (a script file, a variable) isn't refused by them; the OS sandbox still applies to whatever it does.

## The shell's environment

The sandboxed shell starts with `bash --noprofile --norc`, so your shell startup files don't run. A command doesn't inherit the server's environment: it starts from a short allowed list of variables, and anything else stays out, whatever its name. That list is ordinary: `HOME`, `PATH`, `USER`, `SHELL`, `TMPDIR`, the thread's ID, per-thread package caches, `SSH_AUTH_SOCK` and, if you set one up, the agents' GitHub account settings.

## macOS

Shell commands run under [Agent Safehouse](https://github.com/eugene1g/agent-safehouse) 0.11.1 or later, which builds a profile for the macOS `sandbox-exec` facility. CORSIA adds its own denials to that profile for each command (`backend/tools/shell_guard.py`). If Safehouse is missing or too old, the shell refuses with "Nothing ran" rather than running unconfined (`backend/tools/shell_sandbox.py`).

`sandbox-exec` is deprecated by Apple but still enforced by the kernel on current macOS.

## Linux

The same path inventory drives a [bubblewrap](https://github.com/containers/bubblewrap) sandbox (`backend/tools/linux_sandbox.py`, [`docs/linux-sandbox.md`](../linux-sandbox.md)). The command runs in its own user, PID, IPC and UTS namespaces. Your home folder is replaced by an empty in-memory folder with the workspace, package caches and allowed settings mounted into it; `~/.gitconfig` is read-only; shell startup and autostart files are hidden; each thread gets its own `/tmp`; the SSH agent socket is passed through; a fresh keyring is used. There is **no network namespace**: network access is the same as on macOS. Other projects' folders are hidden from the shell ([details](../linux-sandbox.md)). Without bubblewrap the shell refuses to run.

The Linux behavior on this page is read from code and its tests, not run for this page.

## Repositories and worktrees

When a registered repository's base checkout is protected, that main checkout is read-only to agents and work happens in linked worktrees under `.worktrees/`, which CORSIA verifies belong to that repository before granting write access. A small set of Git operations on the main checkout (such as a fast-forward of its branch) runs through a controlled path. A repository can pin a branch or protect its base; an agent that needs an exception asks for it in a lease, and a person decides.

## Check it yourself

Ask any agent to run these in its shell. Each was run from an agent shell on macOS for this page.

```bash
ls ~/Documents               # Operation not permitted
cat ~/.ssh/id_ed25519        # Operation not permitted (or no such file)
sudo -n true                 # Command blocked by protected-mode safety guard (privilege escalation)
env | cut -d= -f1 | sort     # no names containing KEY, TOKEN, SECRET or PASSWORD
curl -sI https://example.com | head -1   # HTTP/2 200: outbound is open
ls ~/.config                 # readable: shows your tool settings folders
```

To see how far the project fence goes, ask an agent in one project to run `ls` on another project's folder in its shell (`Operation not permitted` on macOS), then to list the same folder with its `list_dir` file tool (it lists). Both were run for this page.
