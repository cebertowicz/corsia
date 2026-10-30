# Agent sandbox

How CORSIA confines an agent's shell commands and file tools, on macOS and Linux, without a lease. Leases are on [Host access](host-access.md).

**In short:** agents run as your user. File tools reach only the workspace and registered repositories. Shell commands run inside an OS sandbox that lets them read and write the workspace, read a few tool-settings folders, and reach the network freely. The workspace, not the project, is the boundary.

## Layers

| Layer | What it covers | Strength | Source |
|---|---|---|---|
| File-tool gate | `read_file`, `write_file`, `edit_file`, `grep`, `glob`, `list_dir` | CORSIA code | `backend/tools/filesystem_gate.py`, `backend/tools/sandbox_config.py` |
| Shell sandbox | Every `bash` command and everything it starts | Operating system | `backend/tools/shell_guard.py`, `backend/tools/shell_sandbox.py` |
| Command refusals | A list of commands refused before they run | CORSIA code | `backend/tools/shell_guard.py` (`check_command_safety`) |

The file tools run inside the CORSIA server, so the OS sandbox doesn't apply to them; their own path check does. The shell runs in a separate sandboxed process.

## What's allowed

**Read and write:** the whole workspace (every project, shared files, every thread's working folder) and every registered repository. A registered repository whose base checkout is protected keeps that main checkout read-only, with its verified worktrees under `.worktrees/` read and write; an unprotected repository is read and write throughout.

**Writable outside the workspace, from the shell (macOS):** temporary folders (`/tmp` and the system temp folder, which holds each conversation's package caches), `~/.ssh/known_hosts`, and some developer-tool caches that Agent Safehouse allows, such as `~/.npm`, `~/.cargo` and `~/.rustup`. On Linux, each thread gets its own `/tmp` (below).

**Read only, from the shell:** system files such as `/etc`, and some tool settings in your home folder: `~/.gitconfig`, most of `~/.config`, most of `~/.cache`. The file tools don't reach these.

**Network:** any outbound connection, to the internet or your local network. Not limited. See [Network](network.md).

**SSH:** the shell gets your SSH agent socket (`SSH_AUTH_SOCK`), so keys loaded in your SSH agent can be used to `ssh` or `git push`. The private key files stay unreadable. `~/.ssh/config`, `known_hosts` and `allowed_signers` are readable.

The project is not a boundary. `backend/tools/filesystem_gate.py` states it in code: project scope is not a confidentiality boundary. An agent working on project A can read and change project B. If two bodies of work must stay apart, give each its own workspace and CORSIA install.

## What's refused

| Refused | Strength |
|---|---|
| The rest of your home folder: Documents, Desktop, Downloads, Pictures, Music, Movies, shell history and startup files, `~/Library` data (Mail, Messages, cookies, browser data, Keychains), `~/.ssh` private keys, `~/.aws`, `~/.docker`, `~/.kube`, `~/.gnupg`, `~/.netrc`, other AI tools' folders | Operating system |
| CORSIA's state folder (database, logs, keys, the agents' browser profile) and its checkout's private files | Operating system |
| Writes outside the workspace and registered repositories, including your home folder and `/`, apart from the exceptions under [What's allowed](#whats-allowed) | Operating system |
| CORSIA's startup items (`LaunchAgents`), its restart control files, the generated `.corsia/context` and `.corsia/skills` folders | Operating system |
| Writes to the workspace's Git settings and hooks (`.git/config`, `.git/hooks`) and its `.git` marker, because the server runs Git there outside the sandbox. Agents can't run `git init` at the workspace root. On Linux, this rule doesn't cover a workspace with no `.git` | Operating system for the shell, CORSIA code for file tools |
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

The same path inventory drives a [bubblewrap](https://github.com/containers/bubblewrap) sandbox (`backend/tools/linux_sandbox.py`, [`docs/linux-sandbox.md`](../linux-sandbox.md)). The command runs in its own user, PID, IPC and UTS namespaces. Your home folder is replaced by an empty in-memory folder with the workspace, package caches and allowed settings mounted into it; `~/.gitconfig` is read-only; shell startup and autostart files are hidden; each thread gets its own `/tmp`; the SSH agent socket is passed through; a fresh keyring is used. There is **no network namespace**: network access is the same as on macOS. Without bubblewrap the shell refuses to run.

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

To see that projects are not separated, ask an agent in one project to list another project's folder in the same workspace.
