"""Platform startup/refusal gate shared by protected command callers."""
from __future__ import annotations
import asyncio
import os
import sys


async def startup_error(available, version_error, *, leased=False):
    if sys.platform == 'linux' and not leased:
        from tools.linux_sandbox_probe import sandbox_error
        return await asyncio.to_thread(sandbox_error)
    from host_access.refusal_messages import safehouse_unavailable_refusal
    if not available():
        return f"Error: {safehouse_unavailable_refusal(os.environ.get('PATH'))}"
    error = await asyncio.to_thread(version_error)
    return f'Error: {error}' if error else None


def launch_failed():
    if sys.platform == 'linux':
        from tools.linux_sandbox_probe import invalidate_sandbox_probe
        invalidate_sandbox_probe()


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
