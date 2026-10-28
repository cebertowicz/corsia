"""Platform startup/refusal gate shared by protected command callers."""
from __future__ import annotations
import asyncio
import os
import sys


async def startup_error(available, version_error, *, leased=False, prefix='Error: '):
    if sys.platform == 'linux' and not leased:
        from tools.linux_sandbox_probe import sandbox_error
        return await asyncio.to_thread(sandbox_error)
    from host_access.refusal_messages import safehouse_unavailable_refusal
    if not available():
        return f"{prefix}{safehouse_unavailable_refusal(os.environ.get('PATH'))}"
    error = await asyncio.to_thread(version_error)
    return f'{prefix}{error}' if error else None


def recheck_after_launch_error(returncode, stderr, *, timed_out=False):
    """Request a fresh probe for a possible bwrap setup error, not a verdict."""
    if sys.platform == 'linux' and not timed_out and returncode == 1 and stderr.startswith(b'bwrap: '):
        from tools.linux_sandbox_probe import invalidate_sandbox_probe
        invalidate_sandbox_probe()


async def recheck_after(owned, *, leased=False):
    """Recheck after a foreground result; leased commands don't run under bwrap."""
    if not leased:
        await asyncio.to_thread(recheck_after_launch_error, owned.returncode, owned.stderr,
                                timed_out=bool(owned.timed_out))


def recheck_after_background_failure(record):
    """A background launch's own bwrap error is the first line of its log."""
    if sys.platform != 'linux' or record.lease_id is not None or record.exit_code != 1:
        return
    try:
        with open(record.log_path, 'rb') as log:
            head = log.read(256)
    except OSError:
        return
    recheck_after_launch_error(1, head)


def is_denial(stderr, is_available):
    messages = ('Operation not permitted',)
    if sys.platform == 'linux':
        messages += ('Permission denied', 'Read-only file system')
    return bool(stderr) and any(message in stderr for message in messages) and is_available()


def available_for_denial(mac_available):
    if sys.platform == 'linux':
        import shutil
        return shutil.which('bwrap') is not None
    return mac_available()
