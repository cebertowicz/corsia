"""Where backend-private launch files live, and the legacy temp paths they left.

Sandbox profiles and Claude Code launch files (MCP configs, seed context) must
never be reachable from an agent shell. The shell sandbox grants the temporary
directory, so they live in 0700 directories under the state root, which every
sandbox denies. Files an earlier build left in the temporary directory are swept
at startup.
"""

from __future__ import annotations

import errno
import logging
import os
import shutil
import tempfile
from collections.abc import Callable, Iterable
from pathlib import Path

from tools.host_path_resolution import canonical

log = logging.getLogger(__name__)

GENERATED_PROFILE_PREFIXES = ("corsia-shell-envelope-", "corsia-controlled-git-", "corsia-mcp-envelope-")
LEGACY_HARNESS_MCP_DIR = "corsia-harness-mcp"
LEGACY_SEED_PREFIX = "cc-seed-"

SHELL_ENVELOPE_DIR = "shell-envelopes"
MCP_ENVELOPE_DIR = "mcp-envelopes"
HARNESS_MCP_DIR = "harness-mcp"
SEED_DIR = "cc-seeds"
CLI_INSTALLER_DIR = "cli-installers"
BROWSER_STAGING_DIR = "browser-staging"
REVIEWER_OUTPUT_DIR = "reviewer-output"
# The directories a startup sweep may empty; MCP envelopes are owned by their launcher.
_SWEPT_DIRS = (SHELL_ENVELOPE_DIR, HARNESS_MCP_DIR, SEED_DIR, CLI_INSTALLER_DIR, REVIEWER_OUTPUT_DIR)


def private_runtime_dir(name: str, state_root: Path | None = None) -> Path:
    """Return the state-owned, agent-denied 0700 directory ``name``."""
    if state_root is None:
        from infra import config

        state_root = config.STATE_ROOT
    directory = Path(state_root) / "runtime" / name
    directory.mkdir(parents=True, exist_ok=True, mode=0o700)
    directory.chmod(0o700)
    return directory


def private_mkstemp(name: str, *, prefix: str, suffix: str) -> tuple[int, str]:
    """Create one 0600 file inside the private directory ``name``."""
    return tempfile.mkstemp(prefix=prefix, suffix=suffix, dir=private_runtime_dir(name))


def private_mkdtemp(name: str, *, prefix: str) -> tempfile.TemporaryDirectory[str]:
    """Create a self-cleaning 0700 staging directory inside the private directory ``name``."""
    return tempfile.TemporaryDirectory(prefix=prefix, dir=private_runtime_dir(name))


def generated_profile_temp_roots() -> tuple[Path, ...]:
    """Return every lexical and /private temporary root the legacy files lived in."""
    roots = {Path(tempfile.gettempdir()), Path("/tmp")}
    for root in tuple(roots):
        raw = str(root)
        roots.add(Path(raw.removeprefix("/private") if raw.startswith("/private/") else "/private" + raw))
    return tuple(sorted(roots))


def _is_profile(name: str) -> bool:
    return name.startswith(GENERATED_PROFILE_PREFIXES) and name.endswith(".sb")


def _is_seed(name: str) -> bool:
    return name.startswith(LEGACY_SEED_PREFIX) and name.endswith(".txt")


def _is_harness_config(name: str) -> bool:
    return name.startswith("cc-") and name.endswith(".json")


def _warn_sweep(action: str, path: str | Path, error: OSError) -> None:
    log.warning(
        "private runtime sweep could not %s %s: errno=%s (%s)",
        action, path, error.errno, error.strerror or type(error).__name__,
    )


def _remove_files(directory: Path, wanted: Callable[[str], bool]) -> int:
    """Unlink this user's regular files in ``directory`` whose names are wanted."""
    if directory.is_symlink():
        return 0
    removed = 0
    try:
        entries = list(os.scandir(directory))
    except FileNotFoundError:
        return 0
    except OSError as error:
        _warn_sweep("list", directory, error)
        return 0
    for entry in entries:
        try:
            if (
                wanted(entry.name)
                and entry.is_file(follow_symlinks=False)
                and entry.stat(follow_symlinks=False).st_uid == os.getuid()
            ):
                os.unlink(entry.path)
                removed += 1
        except FileNotFoundError:
            pass
        except OSError as error:
            _warn_sweep("remove", entry.path, error)
    return removed


def _remove_staged_dirs(directory: Path) -> int:
    """Remove this user's staging directories in ``directory``; never follow a link."""
    if directory.is_symlink():
        return 0
    removed = 0
    try:
        entries = list(os.scandir(directory))
    except FileNotFoundError:
        return 0
    except OSError as error:
        _warn_sweep("list", directory, error)
        return 0
    for entry in entries:
        try:
            if entry.is_dir(follow_symlinks=False) and entry.stat(follow_symlinks=False).st_uid == os.getuid():
                shutil.rmtree(entry.path)
                removed += 1
        except FileNotFoundError:
            pass
        except OSError as error:
            _warn_sweep("remove", entry.path, error)
    return removed


def sweep_legacy_temp_files(roots: Iterable[Path] | None = None) -> int:
    """Delete leftover launch files from the pre-fix temp locations, nothing else.

    Delete after the release following v0.4.1; by then upgraded instances have run it.
    """
    removed = 0
    for root in dict.fromkeys(canonical(r) for r in (generated_profile_temp_roots() if roots is None else roots)):
        removed += _remove_files(root, lambda name: _is_profile(name) or _is_seed(name))
        harness = root / LEGACY_HARNESS_MCP_DIR
        removed += _remove_files(harness, _is_harness_config)
        try:
            if not harness.is_symlink():
                harness.rmdir()
        except OSError as error:
            if error.errno not in (errno.ENOTEMPTY, errno.ENOENT):
                _warn_sweep("remove", harness, error)
    return removed


def sweep_private_runtime_files(state_root: Path | None = None) -> int:
    """Empty the private launch directories; no live handle survives a backend start."""
    if state_root is None:
        from infra import config

        state_root = config.STATE_ROOT
    runtime = Path(state_root) / "runtime"
    return sum(_remove_files(runtime / name, lambda _name: True) for name in _SWEPT_DIRS) + _remove_staged_dirs(
        runtime / BROWSER_STAGING_DIR
    )


def sweep_at_startup() -> None:
    """Remove stale launch files; the real temp roots are left alone under test."""
    removed = sweep_private_runtime_files()
    if os.environ.get("CORSIA_TEST_MODE") != "1":
        removed += sweep_legacy_temp_files()
    if removed:
        log.info("removed %d stale private launch file(s)", removed)
