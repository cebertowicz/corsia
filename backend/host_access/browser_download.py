"""Download Chrome with the pinned installer directly into protected private state."""
from __future__ import annotations

import json
import logging
import shutil
import tempfile
import time
from pathlib import Path

from host_access import browser_cli, browser_executables, browser_identity as identity

logger = logging.getLogger(__name__)
NEEDS_CHROME = "Shared browser needs Google Chrome installed (or a verified Chrome for Testing)"


def _discard_cached(root: Path) -> None:
    # The fixed completed-install directory is distinct from in-flight attempts.
    # Marker contents never select a deletion target.
    marker = root / "chrome-download.json"
    marker.unlink(missing_ok=True)
    for home in (root / "chrome-installed",):
        if home.is_symlink():
            home.unlink()
        elif home.is_dir():
            shutil.rmtree(home)


def cached_chrome() -> str | None:
    root = browser_executables.runtime_root()
    marker = root / "chrome-download.json"
    if not marker.exists():
        _discard_cached(root)
        return None
    try:
        metadata = json.loads(marker.read_text())
        if metadata["agent_browser_version"] != browser_executables.PINNED_NATIVE_VERSION:
            raise ValueError("installer pin changed")
        relative = Path(metadata["executable"])
        if relative.is_absolute() or relative.parts[0] != "chrome-installed" or ".." in relative.parts:
            raise ValueError("invalid private installation path")
        executable = (root / relative).resolve(strict=True)
        if not executable.is_relative_to(root) or not executable.is_file():
            raise ValueError("invalid private executable")
        return str(executable)
    except (OSError, ValueError, KeyError, TypeError, IndexError):
        _discard_cached(root)
        return None


def _failed_attempt(home: Path, result: browser_cli.CommandResult | None) -> None:
    if result is not None and result.settlement is not None:
        def settled(task):
            if not task.cancelled() and task.exception() is None and task.result():
                shutil.rmtree(home, ignore_errors=True)
        result.settlement.add_done_callback(settled)
    elif result is None or result.completion != "unknown":
        shutil.rmtree(home, ignore_errors=True)
    else:
        logger.warning("private Chrome installer exit unresolved; retaining its temporary home")


async def install(native: str, socket_dir: Path) -> str:
    """0.31.2 install.rs selects HOME and fetches Google's live Stable manifest.

    The installer pin, not a Chrome version constant, invalidates this private cache.
    A fresh directory prevents its existing-executable shortcut from trusting a partial install.
    """
    if cached := cached_chrome():
        return cached
    root = browser_executables.runtime_root()
    home = Path(tempfile.mkdtemp(prefix="chrome-home-", dir=root))
    started = time.monotonic()
    result = None
    try:
        result = await browser_cli.invoke((native, "install"), socket_dir=socket_dir,
                                         config_path=socket_dir / "config.json", home=home,
                                         timeout=browser_cli.CHROME_INSTALL_TIMEOUT)
        candidates = sorted((home / ".agent-browser" / "browsers").glob("chrome-*/**/Google Chrome for Testing"))
        if result.returncode or result.timed_out or result.completion != "exited" or not candidates:
            logger.warning("private Chrome install failed: completion=%s timed_out=%s exit=%s",
                           result.completion, result.timed_out, result.returncode)
            raise identity.BrowserIdentityError(NEEDS_CHROME)
        executable = candidates[-1].resolve(strict=True)
        if not executable.is_file() or not executable.is_relative_to(home):
            raise identity.BrowserIdentityError(NEEDS_CHROME)
        size = sum(path.stat().st_size for path in home.rglob("*") if path.is_file())
        relative = executable.relative_to(home)
        installed = root / "chrome-installed"
        home.replace(installed)
        home = installed
        executable = installed / relative
        marker = root / "chrome-download.json"
        marker.write_text(json.dumps({"executable": str(executable.relative_to(root)),
                                      "agent_browser_version": browser_executables.PINNED_NATIVE_VERSION}))
        marker.chmod(0o600)
        logger.info("private Chrome installation: bytes=%s elapsed_seconds=%.2f", size, time.monotonic() - started)
        return str(executable)
    except browser_cli.CommandCancelled as exc:
        _failed_attempt(home, exc.result)
        raise
    except BaseException as exc:
        _failed_attempt(home, result)
        if isinstance(exc, (OSError, ValueError, KeyError, TypeError)):
            raise identity.BrowserIdentityError(NEEDS_CHROME + "; try browser setup again") from exc
        raise


def is_private_download(executable: str) -> bool:
    path = Path(executable).resolve(strict=True)
    if not path.is_relative_to(browser_executables.runtime_root()):
        return False
    cached = cached_chrome()
    return cached is not None and Path(cached) == path
