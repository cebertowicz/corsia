"""Prepare an acquired Update release before its restart drain begins."""

from __future__ import annotations

import json
from pathlib import Path

from fastapi import HTTPException


async def prepare_update_before_drain(
    operation_id: str,
    acquired_ids: list[str],
    corsia_script: Path,
    restart_env: dict[str, str],
) -> list[str]:
    """Run the preflight and prepare one owned Update release."""
    from api.settings_restart import (
        _check_update_backup_room,
        _compensate_preparation_failure,
        _prepare_owned_release,
    )
    try:
        await _check_update_backup_room()
    except Exception as error:
        from infra.database_migrations import BackupRoomError

        if not isinstance(error, BackupRoomError):
            raise
        detail = json.dumps({
            "needed_bytes": error.needed_bytes,
            "free_bytes": error.free_bytes,
        })
        await _compensate_preparation_failure(
            operation_id,
            error,
            failure_code="disk_space",
            failure_detail=detail,
        )
        raise HTTPException(
            409,
            {"code": "disk_space", "needed_bytes": error.needed_bytes,
             "free_bytes": error.free_bytes},
        ) from error
    await _prepare_owned_release(operation_id, acquired_ids, corsia_script, restart_env)
    return ["restart-prepared"]
