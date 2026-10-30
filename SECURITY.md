# CORSIA Security Posture

Last reviewed: 2026-10-28

## Active Protections

### Agent Sandbox

- **Protected tools** — filesystem tools gate paths using the shared sandbox configuration. Safehouse (macOS) or bubblewrap (Linux) wraps shell calls. On Linux the protected shell runs in separate user, PID, IPC and UTS namespaces, with bind mounts and masked paths built from the same path inventory, and refuses to run without bubblewrap.
- **Workspace and repositories** — the configured workspace and registered repositories form the ordinary access scope. Protected base checkouts are read-only; their verified `.worktrees` are read/write.
- **Private state and commands** — protected mode denies access to the CORSIA state root and private roots. A reviewed, time-bounded host-access lease can open the state root for a specific action. The key store (`<state root>/keys`, a 0700 folder of 0600 files) and the saved browser profile are never opened, by a lease or otherwise. Protected mode refuses commands that invoke privileged tools including `sudo`, `su`, `shutdown` and `pmset`; on macOS the Safehouse profile also denies executing those binaries directly.
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
- **Devices** — an opt-in TLS listener (default port 47901) for pairing other devices. It accepts signed-in person sessions only, refuses peers outside private networks unless an outside address is configured, and can be managed only from the main computer itself.
- **Remote access** — CORSIA includes guidance for setting up a Tailscale Serve mapping to the local service.

## Gaps / Future Work

- **No container isolation** — Safehouse (macOS) and bubblewrap (Linux) restrict files and commands; neither is a container boundary.
- **No general network egress control** — agents can make outbound network requests; the sandbox does not restrict general network egress.
- **No lock integrity check** — `backend/requirements.lock` is installed without `--require-hashes`.
- **No sign-in lockout or second factor** — repeated failed sign-ins are slowed, not locked out, and there is no multi-factor authentication.
- **API token expiry** — API tokens do not expire automatically; they can be revoked by token or label.
