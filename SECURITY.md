# CORSIA Security Posture

Last reviewed: 2026-03-26

## Active Protections

### Agent Sandbox
- **Safehouse CLI wrapping** — every `bash` tool call runs through `safehouse` with `--add-dirs` restricted to `~/.corsia/`
- **File tool path gating** — `read_file`, `write_file`, `edit_file`, `list_dir`, `glob` all check paths against `sandbox_config.py`
- **Single config source** — `backend/tools/sandbox_config.py` defines allowed directories for both layers
- **Denial alerting** — blocked commands produce orange system messages in the UI via WebSocket broadcast
- **What's blocked**: `~/.ssh/` (private keys), `~/.aws/`, `~/Documents/`, `~/.zshrc`, any path outside `~/.corsia/`
- **What's allowed**: Full access within `~/.corsia/`, SSH `known_hosts`/`config` (git needs them), `/tmp` writes

### Dependency Pinning
- **CORSIA venv** (`~/.corsia/.venv/`) — dedicated, not shared with any other tool
- **`requirements.txt`** — pins critical packages (litellm==1.81.15)
- **`requirements.lock`** — pins all 80 transitive dependencies with exact versions
- **`package-lock.json`** — npm lockfile for frontend

### Automated Auditing
- **Daily security audit** — `scripts/security-audit.sh` runs via launchd at 6am
- **Covers**: CORSIA venv (pip-audit), pipx venvs, Homebrew Python site-packages, npm (frontend)
- **Reports**: `STATE_ROOT/security/audit-YYYY-MM-DD.md` (30-day retention)
- **Activation**: `cp ~/.corsia/scripts/com.corsia.security-audit.plist ~/Library/LaunchAgents/ && launchctl load ~/Library/LaunchAgents/com.corsia.security-audit.plist`

### Authentication
- **Accounts** — every install has its own accounts; there is no shipped or default credential. A username and a password authenticate a person; a token authenticates a request.
- **Password storage** — scrypt (N=2^17, r=8, p=1) with a per-account salt, minimum 8 characters. Only the derived hash is stored.
- **Account creation is CLI-only** — `corsia user add`. The backend binds `0.0.0.0`, so a first-run web form would hand the install to whoever reached the port first; running a command proves local access to the machine.
- **Token storage** — tokens are random, and only their SHA-256 digest is stored. A token value is shown once at creation and cannot be recovered afterwards.
- **Token kinds** — `session` (30 days, issued by sign-in), `service` (1 hour, minted by the lifecycle script for its own authenticated calls), `api` (no expiry, for agent tooling). Expired rows are deleted whenever a token is issued, and an expired token is refused regardless.
- **Sign-in** — `POST /api/auth/login`. Password checks are serialized behind one lock and run off the event loop, and repeated failures for a username get a growing delay answered with 429 + `Retry-After`, so an unauthenticated caller cannot exhaust memory or grind guesses in parallel.
- **Revocation** — `corsia token revoke <label>`, or `POST /api/auth/logout` for the current session. Changing a password revokes that account's sessions and deliberately keeps its API tokens, which belong to installed tooling rather than to a browser.
- **Coverage** — HTTP routes, agent tool calls and WebSocket handshakes resolve their caller through one seam. An unknown and an expired credential are answered identically (`Invalid or expired session`).
- **Network exposure** — the backend listens on `0.0.0.0`, so it is reachable from the local network; authentication, not the bind address, is what protects it. Remote access is intended to go through Tailscale (HTTPS on port 8443, requires Tailscale auth).

## Known Vulnerabilities (as of 2026-03-26)

| Package | Version | CVE | Severity | Fix Available | Environment | Action |
|---------|---------|-----|----------|---------------|-------------|--------|
| pygments | 2.19.2 | CVE-2026-4539 | Low | No | CORSIA venv | Wait for upstream fix (ReDoS in unused AdlLexer) |
| cookie | <0.7.0 | GHSA-pxg6-pf52-xh8x | Low | Breaking only | npm (via SvelteKit) | Wait for SvelteKit to update upstream |
| pdfminer-six | 20251107 | CVE-2025-70559 | — | 20251230 | Homebrew Python 3.14 | `pip3 install --upgrade pdfminer-six` |
| pymupdf | 1.26.5 | CVE-2026-3029 | — | 1.26.7 | Homebrew Python 3.14 | `pip3 install --upgrade pymupdf` |

### Resolved
| Date | What | How |
|------|------|-----|
| 2026-03-26 | picomatch ReDoS (npm, high) | `npm audit fix` |
| 2026-03-26 | undici WebSocket overflow/smuggling (npm, high) | `npm audit fix` |
| 2026-03-26 | devalue prototype pollution (npm, moderate) | `npm audit fix` |

## Pending Actions

- [x] **Uninstall nanobot-ai pipx venv** — 9 CVEs in an unused environment. ✅ Done 2026-03-26
- [x] **Activate daily audit cron** — Run: `cp ~/.corsia/scripts/com.corsia.security-audit.plist ~/Library/LaunchAgents/ && launchctl load ~/Library/LaunchAgents/com.corsia.security-audit.plist`
- [x] **Upgrade Homebrew Python packages** — pdfminer-six and pymupdf have fixes available
- [x] **Clean up nanobot references in code** — dead import path in `settings.py`, stale comments in `agent.py`, `main.py`, `state_context.py`. ✅ Done 2026-03-26

## Gaps / Future Work

- **No container isolation** — agents run as the owner's user. Safehouse is a soft sandbox (filesystem + binary restrictions), not a hard boundary. Full containment would require Docker/Podman or Apple Containers.
- **No network egress control** — agents can make arbitrary HTTP requests. Could be mitigated with Lulu firewall rules or a proxy.
- **No integrity verification** — no checksum validation on `requirements.lock` (could detect tampering)
- **Homebrew audit blocked by sandbox** — `brew outdated` can't run inside Safehouse (needs cache dirs). Must be run manually.
- **Token rotation** — revocation exists (`corsia token revoke`, sign-out, password change) but rotation is manual: nothing expires an `api` token or prompts for a periodic change.
- **No multi-factor or lockout** — a failed sign-in is slowed by a growing per-username delay, not locked out, and there is no second factor.
- **Audit alerting** — daily reports are written to disk but not surfaced proactively (no email/notification on new CVEs)
- **Code signing** — no verification that backend code hasn't been tampered with between restarts
