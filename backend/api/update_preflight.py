"""Checks that must finish before an Update begins its durable drain."""

from __future__ import annotations

import asyncio
import logging
import plistlib
from collections.abc import Callable
from pathlib import Path
from typing import TYPE_CHECKING
from xml.parsers.expat import ExpatError

from fastapi import HTTPException

if TYPE_CHECKING:
    from infra.update_status import UpdateStatus

logger = logging.getLogger(__name__)

# The Info.plist key the release pipeline stamps with the full release version.
# CFBundleShortVersionString cannot hold it: macOS requires that key to be
# numeric, so a dev build's `0.4.1-dev.N` never appears there.
_BUNDLE_VERSION_KEY = "CORSIAVersion"

#: What reading one staged bundle's stamped version can fail as: no bundle or an
#: unreadable file, plist XML that will not parse, a value that is not the plist's
#: string version, or a version string semver cannot parse.
_STAGED_VERSION_ERRORS = (ExpatError, OSError, TypeError, ValueError)


def with_failure_code(failure: HTTPException, code: str) -> HTTPException:
    """Name the durable cause the Update journey shows for this failure."""
    setattr(failure, "update_failure_code", code)
    return failure


async def fresh_update_target() -> UpdateStatus:
    """Probe the feed fresh and return the newer build an Update will install.

    A packaged install verifies the running bundle against the stamped target, so
    a probe that names no build has nothing to verify: refuse before the drain,
    or whatever the shell installs could only fail and roll a good build back. A
    source checkout compares nothing, so it keeps `corsia prepare-redeploy`'s own
    specific causes and durable row and is never refused here; its stamp is the
    fresh probe's target, or None when the probe has none.
    """
    from infra import platform_paths
    from infra.update_status import update_status

    fresh = await asyncio.to_thread(update_status, True)
    if fresh.available is not True and platform_paths.app_bundle_root() is not None:
        logger.warning(
            "update refused before acquire: available=%s error=%s", fresh.available, fresh.error
        )
        if fresh.error:
            raise HTTPException(503, "update_check_failed")
        raise HTTPException(409, "update_not_available")
    return fresh


def _staged_bundle_target() -> str | None:
    """Return the version the staged bundle carries, or None when it cannot be read.

    The updater unpacks the build it just downloaded at ``CORSIA.app.staged`` beside
    the installed app, and the swap installs that copy. Its stamped
    ``CORSIAVersion`` is the same string the frontend stamp reports at runtime, so
    it is exactly what post-swap verification will see running.
    """
    from infra import platform_paths
    from infra.update_status import normalize_packaged_version

    bundle = platform_paths.app_bundle_root()
    if bundle is None:
        return None
    staged = bundle.with_name(f"{bundle.name}.staged")
    try:
        with (staged / "Contents" / "Info.plist").open("rb") as handle:
            payload = plistlib.load(handle)
        version = payload.get(_BUNDLE_VERSION_KEY) if isinstance(payload, dict) else None
        if not isinstance(version, str):
            raise TypeError(f"staged bundle omits {_BUNDLE_VERSION_KEY}")
        return normalize_packaged_version(version)
    except _STAGED_VERSION_ERRORS as exc:
        logger.warning("staged bundle version could not be read from %s: %s", staged, exc)
        return None


async def restamp_staged_target(operation_id: str, discard_staged: Callable[[], None]) -> None:
    """Stamp an acquired Update with the build staging left, before any drain.

    Staging reads the feed again and can take minutes, so a build published after
    the pre-drain probe is the one the swap installs; verifying against the probed
    version would roll that good build back. The staged bundle is the only record
    of what installs, so an Update that cannot read its version must not reach the
    swap: the staged build is discarded and preparation fails with its own cause.
    A source checkout stages no bundle and verifies no version, so its probed stamp
    stands.
    """
    from automation import self_restart
    from infra import platform_paths
    from infra.db_async import run_in_db

    if platform_paths.app_bundle_root() is None:
        return
    staged = await asyncio.to_thread(_staged_bundle_target)
    if staged is not None:
        if await run_in_db(self_restart.restamp_target_version, operation_id, staged):
            return
        raise with_failure_code(
            HTTPException(
                409,
                "Update stopped before restart: the staged build's version could not be recorded.",
            ),
            "update_refused",
        )
    reason = "the staged build's version could not be read from its bundle"
    try:
        await asyncio.to_thread(discard_staged)
    except Exception as discarded:  # cleanup never replaces the cause
        reason += f"; the staged build could not be removed either ({discarded})"
    raise with_failure_code(
        HTTPException(409, f"Update stopped before restart: {reason}."),
        "staged_bundle_unverifiable",
    )


def _check_update_backup_room() -> None:
    """Refuse an update when its startup migration backup cannot fit."""
    from infra.database import get_db
    from infra.database_migrations import require_backup_room

    connection = get_db()
    row = connection.execute("PRAGMA database_list").fetchone()
    source = Path(row[2]) if row and row[2] else None
    if source is None:
        return
    version = int(connection.execute("PRAGMA user_version").fetchone()[0])
    require_backup_room(source, version)


async def check_update_backup_room() -> None:
    """Run the durable pre-drain room check on CORSIA's SQLite work lane."""
    from infra.db_async import run_in_db

    await run_in_db(_check_update_backup_room)
