# Protected commands on Linux

Use the ordinary shell and script tools for project work. CORSIA runs protected commands with bubblewrap 0.8.0 or newer. The existing workspace, repository and additional-directory settings determine writable paths. Each chat gets a private, persistent `/tmp`, removed when the chat closes.

## Sandbox setup

If bubblewrap is missing or its startup check fails, protected commands do not run. On Ubuntu 24.04 or later, run `bash scripts/install-linux-sandbox.sh` on this computer to install bubblewrap and, when required, its packaged restrictive AppArmor profile. The CORSIA installer also runs this check and stops if it cannot set up the sandbox. Check the diagnostic: upgrade an outdated bubblewrap binary, and ensure the distribution provides the `bwrap-userns-restrict` profile in the `apparmor-profiles` package. If setup still fails, use a supported distribution with that profile, such as Ubuntu 24.04. Restart CORSIA after setup. Do not bypass a refusal by running the command without the sandbox.

Updates to CORSIA's managed browser require administrator access and must be installed through the installer or updater. Where AppArmor restricts unprivileged user namespaces (Ubuntu 23.10 and later), the installer adds an AppArmor rule that lets the managed browser use its own sandbox; Debian needs none. To remove that rule, run `sudo apparmor_parser -R /etc/apparmor.d/corsia-chromium && sudo rm /etc/apparmor.d/corsia-chromium`.

Workspace writes, linked-worktree commits, branch creation, fetches, rebases, stashes and pushes can use the protected shell. SSH uses a forwarded agent socket; private key files remain hidden. Generated workspace context, protected repository controls, private state and persistence paths remain protected even when an additional writable directory contains them. DNS and ordinary network access remain available.

Grants for `/dev` or `/proc` retain the minimal devices and private process view. A grant for `/tmp` retains the chat's private temp directory. Grant specific project directories instead of `/`; a root filesystem grant is refused.

Some writes require a different target:

- A broad home-directory grant freezes existing ancestors of absent protected paths. New entries at those ancestors cannot be created. Existing tool caches remain writable; an absent cache can use a writable project directory, such as `UV_CACHE_DIR="$PWD/.cache/uv"` or `npm ci --cache "$PWD/.cache/npm"`.
- Protected repositories keep the common Git directory read-only while allowing existing object, ref, log and linked-worktree metadata directories. A base-checkout fetch cannot write `FETCH_HEAD`. Root-level locks such as `packed-refs.lock` cannot be created. Git maintenance can fail, and rebase or stash can emit a maintenance warning despite completing their primary operation. Inspect the Git result before continuing.
- The workspace's own Git control paths stay read-only: `.git/config`, `.git/commondir`, `.git/config.worktree` and `.git/hooks`, wherever the workspace's Git directory lives. With no repository at the workspace, CORSIA creates one before the command runs so those paths can be protected; when an enclosing repository prevents that, CORSIA creates an empty `.git` directory instead, which Git ignores, and no command can add entries inside it. A control file that already exists is never rewritten. If CORSIA cannot make one of these paths, the command does not run.
- Controlled merges into a protected base checkout are refused on Linux. Work in a linked worktree and use the repository's approved integration process.

Each protected command joins a fresh anonymous session keyring before bubblewrap starts; the host's session and user keyrings remain inaccessible. The host network namespace is shared. Local services must authorize requests themselves. A read-only service response is not evidence that a privileged service operation is allowed. On a machine with a graphical desktop session, protected commands can reach that session's display server, so run CORSIA on a headless computer or under its own user account.
