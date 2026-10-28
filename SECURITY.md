# CORSIA Security Posture

Last reviewed: 2026-10-28

## Active Protections

### Agent Sandbox

- **Protected tools** — Safehouse wraps shell calls, and filesystem tools gate paths using the shared sandbox configuration.
- **Workspace and repositories** — the configured workspace and registered repositories form the ordinary access scope. Protected base checkouts are read-only; their verified `.worktrees` are read/write.
- **Private state and commands** — protected mode denies access to the CORSIA state root and private roots and blocks direct execution of privileged commands including `sudo`, `su`, `shutdown` and `pmset`.
- **SSH** — protected mode blocks SSH private material while allowing reads of SSH configuration and reads and writes of `known_hosts`.

### Dependency Pinning

- **Python** — the installer creates a dedicated virtual environment and installs from `backend/requirements.lock`. The lock pins versions; `litellm` is pinned to a Git commit in `backend/requirements.txt` and the lock.
- **Frontend** — `frontend/package-lock.json` is the npm lockfile.

### Security Auditing

- **Manual audit** — a maintainer or operator can run `scripts/security-audit.sh`. It audits the CORSIA Python environment, available pipx and Homebrew Python environments, and the frontend npm dependencies when present; it also checks for outdated Homebrew packages.
- **Effects and reports** — the script attempts npm and Python dependency fixes, writes a dated report and alert state under the selected state root's `security` directory, and removes reports older than 30 days.

### Authentication

- **First account** — create it with `corsia user add` or the pre-auth first-run route, which accepts direct loopback requests only and exists only while no account exists.
- **Passwords** — new password hashes use scrypt (N=2^17, r=8, p=1) with a fresh salt. Passwords must be at least eight characters.
- **Tokens** — only SHA-256 digests are stored. Session tokens last 30 days, service tokens one hour, and API tokens have no expiry.
- **Sign-in and revocation** — password checks are serialized; repeated failures for a username incur a growing delay. Sign-out revokes the presented token, and tokens can also be revoked by label. Unknown and expired tokens receive the same authentication response.

### Network Exposure

- **Bind address** — setup selects local (`127.0.0.1`) or server (`0.0.0.0`) access. A mismatch between the recorded choice and bind address fails closed. Existing installations without a recorded choice retain the legacy server bind.
- **Remote access** — CORSIA includes guidance for setting up a Tailscale Serve mapping to the local service.

## Gaps / Future Work

- **No container isolation** — agents run as the person's own user. Safehouse is a macOS sandbox profile that restricts files and commands; it is not a container boundary.
- **API token expiry** — API tokens do not expire automatically; they can be revoked.
