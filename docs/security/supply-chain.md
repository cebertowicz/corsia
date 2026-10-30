# Supply chain

Where CORSIA's code and its dependencies come from, what is pinned or signed, how updates arrive, and what agents can install.

**In short:** dependencies are pinned to exact versions but not to hashes. The release workflow builds an unsigned desktop app, and signing is a separate step run by hand, so the repository doesn't show whether a given download is signed. Updates pull the branch you installed from, without signature checks, and run only when you start them or turn on automatic updates. Shared skills are checked file by file against the library's index; the library's own items follow its `main` branch, and items from other repositories are pinned to a commit.

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

**Checking for known vulnerabilities** is manual: `scripts/security-audit.sh` runs `pip-audit` on CORSIA's Python environment and other Python environments it finds (pipx, Homebrew), runs `npm audit fix --omit=dev` on the web app (this **changes** `frontend/` packages), lists outdated Homebrew packages, and writes a dated report to the state folder's `security` folder, deleting reports older than 30 days. `pip-audit` needs installing into CORSIA's environment first. Nothing runs it on a schedule or in CI.

## The desktop app and releases

- The release workflow (`.github/workflows/release-macos.yml`) builds an **unsigned** app. Signing and notarization happen in a separate script run by hand (`desktop/scripts/sign-macos-bundle.sh`).
- Nothing in the repository records whether a published download (such as the GitHub pre-release named `dev-channel`) was signed and notarized before upload. Check a download yourself before trusting it (below), or build from source.
- Release tags `v0.1.0` to `v0.3.0` are annotated tags with no signature (`git verify-tag` answers "no signature found"). The `v0.4.1-dev.*` tags are lightweight: they name a commit and carry no signature of their own. Commits merged through GitHub's website carry GitHub's own signature; other commits aren't signed (checked for this page).

## Updates

Updates run when you start one, or at a time you choose if you turn on automatic updates (off by default; `backend/infra/settings.py`). An update (`scripts/corsia.d/runtime.sh`):

1. Runs `git pull --ff-only` on the branch the checkout tracks, from the GitHub repository it was cloned from. It refuses if the branch has diverged. It checks no signatures.
2. Runs `pip install -r backend/requirements.lock` and `npm install` as above.
3. Rebuilds and restarts.

Anyone who can push to the branch you track can change what your install runs on its next update.

## Shared skills and setups

The skill library is a separate public repository. It is **off** until you agree to use community improvements in your profile (`backend/tools/skill_library.py`). Once on:

- Agents can search it and install a skill into their own conversation without asking you.
- The library's own items are read from its `main` branch, and each file is checked against the SHA-256 recorded in the library's index, which is read from `main` too. The check catches a file that doesn't match the index, not a change by someone who can push to the library. Items the index lists from other repositories are pinned to a Git commit (`backend/library/items.py`, `backend/library/index.py`).
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
git verify-tag v0.3.0
```

`error: no signature found` means the tag carries no signature of any kind (checked for this page). For a tag that `git cat-file -t` reports as `commit` rather than `tag`, there is no tag object to sign; this command then checks nothing.

On macOS, for a downloaded desktop app:

```bash
spctl --assess --type execute --verbose /Applications/CORSIA.app
```

`accepted` with `source=Notarized Developer ID` means the app is signed and notarized; `rejected` means macOS doesn't accept it (checked for this page on an installed app). Use the path where you put the app.
