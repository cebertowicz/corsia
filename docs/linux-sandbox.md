# Protected commands on Linux

Use the ordinary shell and script tools for project work. CORSIA runs protected commands with bubblewrap 0.8.0 or newer. The existing workspace, repository and additional-directory settings determine writable paths. Each thread gets a private, persistent `/tmp`, removed when the thread closes.

If bubblewrap is missing or its startup check fails, the command does not run. Follow the returned installation guidance on the server; on Ubuntu, `bash scripts/install-linux-sandbox.sh` installs bubblewrap and, when required, the packaged restrictive AppArmor profile. The Linux installer invokes this helper during server setup. Check the diagnostic first: an outdated bubblewrap binary needs an upgrade; an Ubuntu user-namespace denial can require the restrictive AppArmor profile. Restart CORSIA after setup. Do not bypass a refusal by running the command without the sandbox.

Workspace writes, linked-worktree commits, branch creation, fetches, rebases, stashes and pushes can use the protected shell. SSH uses a forwarded agent socket; private key files remain hidden. Generated workspace context, protected repository controls, private state and persistence paths remain protected even when an additional writable directory contains them. DNS and ordinary network access remain available.

Grants for `/dev` or `/proc` retain the minimal devices and private process view. A grant for `/tmp` retains the thread's private temp directory. Grant specific project directories instead of `/`; a root filesystem grant is refused.

Some writes require a different target:

- A broad home-directory grant freezes existing ancestors of absent protected paths. New entries at those ancestors cannot be created. Existing tool caches remain writable; an absent cache can use a writable project directory, such as `UV_CACHE_DIR="$PWD/.cache/uv"` or `npm ci --cache "$PWD/.cache/npm"`.
- Protected repositories keep the common Git directory read-only while allowing existing object, ref, log and linked-worktree metadata directories. A base-checkout fetch cannot write `FETCH_HEAD`. Root-level locks such as `packed-refs.lock` cannot be created. Git maintenance can fail, and rebase or stash can emit a maintenance warning despite completing their primary operation. Inspect the Git result before continuing.
- Controlled merges into a protected base checkout are refused on Linux. Work in a linked worktree and use the repository's approved integration process.

Each protected command joins a fresh anonymous session keyring before bubblewrap starts; the host's session and user keyrings remain inaccessible. The host network namespace is shared. Local services must authorize requests themselves. A read-only service response is not evidence that a privileged service operation is allowed.
