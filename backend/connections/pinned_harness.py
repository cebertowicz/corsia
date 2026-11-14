"""Install CORSIA's own checked copy of Claude Code and Codex.

Agents never run the person's copy (``connections.PINNED_HARNESSES``). The copy is
fetched only for a vendor this install uses, so a local-only setup downloads nothing.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Callable
from pathlib import Path

from connections import PINNED_HARNESSES

log = logging.getLogger(__name__)


def install_pinned(vendor: str, *, should_stop: Callable[[], bool] | None = None) -> Path:
    """Install this machine's pinned copy if missing, drop older versions, return the executable.

    Raises ``ManagedRuntimeError`` (``ManagedRuntimeCancelled`` once ``should_stop`` says
    so); the caller turns it into its own failure state.
    """
    from tools.managed_runtime import ensure_runtime, machine_key, remove_other_versions

    executable = ensure_runtime(vendor, arch=machine_key(), should_stop=should_stop)
    remove_other_versions(vendor)
    return executable


def vendor_in_use(vendor: str, *, db=None) -> bool:
    """The person has this vendor's own CLI, or a signed-in CORSIA account for it.

    A signed-out account, or the ``legacy`` Default every migrated install got for each
    seeded connection, is not use: counting it would download both CLIs for a local-only person.
    """
    from cli_location import service_binary
    from connections.vendor_accounts import list_accounts

    if service_binary(vendor):
        return True
    return any(account["state"] == "signed_in" for account in list_accounts(provider=vendor, db=db))


async def install_if_used(vendor: str) -> bool:
    """Install the pinned copy when it is missing and this install uses the vendor.

    True when a copy was installed now. A failed download raises ``ManagedRuntimeError``.
    """
    from infra.db_async import run_in_db
    from tools.managed_runtime import installed_runtime_path

    if installed_runtime_path(vendor) is not None or not await run_in_db(vendor_in_use, vendor):
        return False
    await asyncio.to_thread(install_pinned, vendor)
    return True


async def install_used_harnesses() -> None:
    """After startup, off its path: install the pinned copy for each vendor in use.

    A copy installed now is recorded on its row and its accounts' catalogs are read,
    since startup discovery ran without it. A failure is logged and leaves the row
    ``not_installed``; nothing retries here. The next start, the Install button, or
    the next re-check (``check_harness_source``) tries again.
    """
    from connections.source_state import check_harness_source
    from tools.managed_runtime import installed_runtime_path

    for vendor in PINNED_HARNESSES:
        try:
            if installed_runtime_path(vendor) is not None:
                await asyncio.to_thread(install_pinned, vendor)  # an installed copy only drops older versions
            elif await install_if_used(vendor):
                await check_harness_source(vendor)
        except Exception as exc:  # the row says not installed
            log.warning("could not set up CORSIA's %s copy: %s", vendor, exc)
