"""Fail-closed Linux sandbox startup check.

Only successful checks are cached; a change to the bwrap binary triggers a new check.
"""

from __future__ import annotations

import logging
import os
import re
import shutil
import subprocess
import threading

from tools.linux_keyring_launcher import launcher_argv
from tools.linux_sandbox import NAMESPACES

logger = logging.getLogger(__name__)

_MISSING = (
    'Error: the Linux sandbox (bubblewrap) is not installed on this computer. '
    'Nothing ran. Install it with "sudo apt install bubblewrap", then restart CORSIA.'
)
_FAILURE = (
    'Error: the Linux sandbox (bubblewrap) is installed but could not complete its startup check. '
    'Nothing ran. On Ubuntu 24.04 or later, rerun the CORSIA installer to add '
    'its AppArmor rule, then restart CORSIA.'
)
_lock = threading.Lock()
_success: tuple[object, ...] | None = None


def invalidate_sandbox_probe() -> None:
    """Forget successful startup after a runtime failure."""
    global _success
    with _lock:
        _success = None


def _identity(path: str) -> tuple[object, ...]:
    stat = os.stat(path)
    return (path, stat.st_dev, stat.st_ino, stat.st_size, stat.st_mtime_ns, stat.st_ctime_ns)


def _failure(detail: str) -> str:
    logger.warning('Linux sandbox startup check failed: %s', detail)
    return _FAILURE


def sandbox_error() -> str | None:
    """Return the approved refusal text, or None when bwrap can start."""
    global _success
    with _lock:
        path = shutil.which('bwrap')
        if path is None:
            _success = None
            return _MISSING
        try:
            identity = _identity(path)
            if identity == _success:
                return None
            _success = None
            version = subprocess.run([path, '--version'], capture_output=True, text=True, timeout=5)
            if version.returncode:
                return _failure(version.stderr or 'could not check bubblewrap version')
            match = re.search(r'\b(\d+)\.(\d+)\.(\d+)\b', version.stdout)
            if match is None:
                return _failure('could not determine bubblewrap version')
            if tuple(map(int, match.groups())) < (0, 8, 0):
                return _failure(f'bubblewrap 0.8.0 or newer is required (found {match.group(0)})')
            result = subprocess.run(
                launcher_argv([path, *NAMESPACES, '--ro-bind', '/', '/', '--proc', '/proc', '--', '/bin/true']),
                capture_output=True, text=True, timeout=5,
            )
            if result.returncode:
                return _failure(result.stderr or 'startup probe failed')
            if _identity(path) != identity:
                return _failure('bubblewrap changed during startup verification')
            _success = identity
            return None
        except (OSError, subprocess.SubprocessError) as exc:
            return _failure(str(exc) or type(exc).__name__)
