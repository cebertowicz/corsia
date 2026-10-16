"""Where a vendor CLI lives, and whether the launchd service can see it.

One owner for the vendor -> binary map, the service PATH, and the login-shell
probe. The install route, the link route and ``corsia sources`` all answer the
same two questions with these helpers and keep no copy of their own. Standard
library only: the CLI process imports it too.
"""

from __future__ import annotations

import os
import shutil
import signal
import subprocess
from pathlib import Path

BINARIES = {"claude-code": "claude", "codex": "codex", "omlx": "omlx"}

_BEGIN = "__CORSIA_BEGIN__"
_END = "__CORSIA_END__"
_SHELL_TIMEOUT_SECONDS = 5


def service_path() -> str:
    """The executable PATH the launchd service runs with.

    ``SERVICE_PATH`` in ``scripts/corsia.d/service.sh`` is the source of this
    default; the two must match. ``CORSIA_SERVICE_PATH`` overrides it.
    """
    return os.environ.get("CORSIA_SERVICE_PATH") or ":".join((
        str(Path.home() / ".local/bin"), "/opt/homebrew/bin", "/usr/local/bin",
        "/usr/bin", "/bin", "/usr/sbin", "/sbin",
    ))


def service_binary(vendor: str) -> str | None:
    """The vendor's CLI as the service resolves it, or None when the service cannot see it."""
    binary = BINARIES.get(vendor)
    return shutil.which(binary, path=service_path()) if binary else None


def _shell_value(command: str) -> str | None:
    """Read one marked value from the person's login shell."""
    shell = os.environ.get("SHELL") or "/bin/zsh"
    wrapped = f"printf '%s\\n' {_BEGIN}; {command}; printf '%s\\n' {_END}"
    try:
        process = subprocess.Popen(
            [shell, "-ilc", wrapped], stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL, text=True, start_new_session=True,
        )
        output, _ = process.communicate(timeout=_SHELL_TIMEOUT_SECONDS)
    except OSError:
        return None
    except subprocess.TimeoutExpired:
        os.killpg(process.pid, signal.SIGKILL)
        process.communicate()
        raise TimeoutError from None
    try:
        candidate = output.split(_BEGIN, 1)[1].split(_END, 1)[0].strip()
    except IndexError:
        return None
    return candidate or None


def shell_binary(binary: str) -> str | None:
    """Ask the person's login shell where a binary is; raises TimeoutError if it hangs."""
    return _shell_value(f"command -v {binary}")


def login_shell_path() -> str | None:
    """Return the person's login-shell PATH once, without sourcing it into this process."""
    return _shell_value("printf '%s\\n' \"$PATH\"")


def validated_binary(binary: str, candidate: str | None, home: Path) -> Path | None:
    """The candidate as a resolved executable of that name, else None; never one in ~/.local/bin."""
    if not candidate or not os.path.isabs(candidate):
        return None
    resolved = Path(candidate).resolve()
    if resolved.parent == home / ".local/bin":
        return None
    if resolved.name != binary or not resolved.is_file() or not os.access(resolved, os.X_OK):
        return None
    return resolved


def login_shell_binary(vendor: str) -> Path | None:
    """The vendor's CLI as the login shell finds it, validated; raises TimeoutError if the shell hangs."""
    binary = BINARIES.get(vendor)
    if binary is None:
        return None
    return validated_binary(binary, shell_binary(binary), Path.home())
