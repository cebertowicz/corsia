# Supply chain

Where CORSIA's code and its dependencies come from, what is pinned or signed, how updates arrive, and what agents can install.

**In short:** dependencies are pinned to exact versions but not to hashes. No signed desktop app is published. Updates pull the branch you installed from, without signature checks, and run only when you start them or turn on automatic updates. Shared skills are pinned to a commit and checked file by file.

## Dependencies

| | How it's pinned | Source |
|---|---|---|
| Python packages | Exact versions in `backend/requirements.lock`; **no hashes**, so a replaced file on the package index wouldn't be caught. One package, `litellm`, comes from a fixed Git commit | `backend/requirements.lock` |
| Web app packages | `frontend/package-lock.json`, with integrity hashes. CI uses `npm ci`; the updater uses `npm install --no-save`, which follows the lock file but, unlike `npm ci`, doesn't refuse when it and `package.json` disagree | `scripts/corsia.d/runtime.sh` |
| Desktop app build | `npm ci` | `desktop/scripts/build-macos-bundle.sh` |
| Agents' browser | `agent-browser@0.31` from npm, installed globally by the installer | `scripts/install.sh` |
| Sandbox (macOS) | Agent Safehouse from Homebrew; CORSIA refuses to run shells with a version older than 0.11.1 | `scripts/install.sh`, `backend/tools/shell_guard.py` |
| Linux runtimes | Downloaded by CORSIA and checked against fixed checksums | `backend/tools/managed_runtime.py` |
| Homebrew (macOS, if missing) | Homebrew's own installer from its `HEAD` | `scripts/install.sh` |

No automated dependency-update service (Dependabot or Renovate) is configured.

## The desktop app and releases

- The release workflow (`.github/workflows/release-macos.yml`) builds an **unsigned** app. Signing and notarization happen in a separate script run by hand (`desktop/scripts/sign-macos-bundle.sh`).
- **No signed desktop app is published yet.** A GitHub pre-release named `dev-channel` carries a desktop download; this page didn't check whether it is signed or notarized. Treat it as unsigned.
- Release tags (`v0.1.0` to `v0.3.0`) are not signed. Commits merged through GitHub's website carry GitHub's own signature; other commits aren't signed (checked for this page).

## Updates

Updates run when you start one, or at a time you choose if you turn on automatic updates (off by default; `backend/infra/settings.py`). An update (`scripts/corsia.d/runtime.sh`):

1. Runs `git pull --ff-only` on the branch the checkout tracks, from the GitHub repository it was cloned from. It refuses if the branch has diverged. It checks no signatures.
2. Runs `pip install -r backend/requirements.lock` and `npm install` as above.
3. Rebuilds and restarts.

Anyone who can push to the branch you track can change what your install runs on its next update.

## Shared skills and setups

The skill library is a separate public repository. It is **off** until you agree to use community improvements in your profile (`backend/tools/skill_library.py`). Once on:

- Agents can search it and install a skill into their own conversation without asking you.
- Each item is pinned to a Git commit, and each file is checked against a recorded SHA-256 (`backend/library/items.py`).
- CORSIA checks daily for newer versions but never updates an installed copy on its own.
- Contributions from outside the project aren't open yet, and no review checklist for library items is published.

A skill is instructions, and its bundle can include scripts. An installed skill can tell an agent to do anything that agent can already do.

**System bulletins**, notices that tell agents about changes to CORSIA's own instructions, are fetched only if signed by a release key. No keys ship yet, so the live feed is off (`backend/infra/bulletins.py`).

## What agents can install

| | In the agent shell | Under a host-access lease |
|---|---|---|
| Packages into a project's own environment in the workspace (`pip install` into a venv, `npm install` in a project folder) | Yes, with per-conversation package caches | Yes |
| Global installs (`brew install`, `npm install -g`, `/usr/local`) | No: those folders aren't writable (checked for this page) | Yes, if the reviewers allow it ([Host access](host-access.md)) |
| A tool server | Only by proposing one, which waits for your approval ([Connected apps](connected-apps.md)) | Same |

Package installs inside the workspace run the package's own install scripts with the agent's access, and the network isn't limited ([Network](network.md)).

## Check it yourself

In your CORSIA checkout:

```bash
grep -c "hash=sha256" backend/requirements.lock
```

`0` means the Python lock has no hashes (checked for this page).

In your CORSIA checkout (`~/.corsia` for an install from source):

```bash
git cat-file -p v0.3.0 | grep -c "BEGIN PGP"
```

`0` means the release tag carries no signature (checked for this page).
