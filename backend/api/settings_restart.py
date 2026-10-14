"""Settings restart preparation, durable acquisition, and child launch handoff."""

from __future__ import annotations

import asyncio
import json
import subprocess
import uuid
from collections.abc import Callable
from pathlib import Path

from fastapi import HTTPException

from api.restart_shell import spawn_restart_lifecycle
from api.update_preflight import check_update_backup_room as _check_update_backup_room
from infra.update_prepare_output import clean_update_output, terminal_update_summary

_UPDATE_REQUEST_LOCK = asyncio.Lock()
_RELEASE_FAILURE_NOTE = ("Restart control could not be released automatically; "
                         "the active operation may still be holding ingress.")
# Exit codes of `corsia prepare-redeploy`; any other nonzero exit is `prepare_failed`.
_PREPARE_EXIT_CODES = {
    41: "fetch_failed",
    42: "build_failed",
    43: "index_locked",
    44: "checkout_detached",
    45: "checkout_dirty",
    46: "restart_pending",
}


def _update_log_path() -> Path:
    """Return an immutable per-attempt log path for one Update request."""
    from infra.config import LOGS_DIR

    return LOGS_DIR / f"update-{uuid.uuid4().hex}.log"


def _failure_summary(failure: BaseException) -> str:
    return str(getattr(failure, "detail", failure))[:500]


def _failure_detail(failure: BaseException) -> str | None:
    log_path = getattr(failure, "update_log_path", None)
    return json.dumps({"log_path": str(log_path)}) if log_path else None


def _with_failure_code(failure: HTTPException, code: str) -> HTTPException:
    """Name the durable cause the Update journey shows for this failure."""
    setattr(failure, "update_failure_code", code)
    return failure


def _prepare_current_release(corsia_script: Path, restart_env: dict[str, str]) -> None:
    """Pull, validate, and stage the serving checkout before any restart."""
    try:
        result = subprocess.run(
            [str(corsia_script), "prepare-redeploy"],
            capture_output=True,
            text=True,
            env=restart_env,
            timeout=600,
            check=False,
        )
    except subprocess.TimeoutExpired as exc:
        raise _with_failure_code(
            HTTPException(504, "Update timed out before restart; CORSIA is still running."),
            "update_timed_out",
        ) from exc
    except OSError as exc:
        raise _with_failure_code(
            HTTPException(500, f"Could not start the update: {exc}"), "prepare_failed"
        ) from exc

    stdout = clean_update_output(result.stdout)
    stderr = clean_update_output(result.stderr)
    log_path = _update_log_path()
    try:
        log_path.parent.mkdir(parents=True, exist_ok=True)
        log_path.write_text(
            "\n".join(part.rstrip() for part in (stdout, stderr) if part.strip()) + "\n"
        )
    except OSError as exc:
        if result.returncode != 0:
            raise _with_failure_code(
                HTTPException(
                    500,
                    "Update stopped before restart, and its full log could not be written; "
                    "CORSIA is still running.",
                ),
                _PREPARE_EXIT_CODES.get(result.returncode, "prepare_failed"),
            ) from exc
    if result.returncode == 0:
        return

    summary = terminal_update_summary(stdout, stderr)
    failure = HTTPException(
        409,
        f"Update stopped before restart: {summary}. Full log: {log_path}",
    )
    _with_failure_code(failure, _PREPARE_EXIT_CODES.get(result.returncode, "prepare_failed"))
    setattr(failure, "update_log_path", log_path)
    raise failure


def _discard_prepared_release(
    corsia_script: Path, restart_env: dict[str, str]
) -> None:
    """Ask the management script to remove one validated, unactivated build."""
    result = subprocess.run(
        [str(corsia_script), "discard-prepared"],
        capture_output=True,
        text=True,
        env=restart_env,
        timeout=30,
        check=False,
    )
    if result.returncode != 0:
        raise RuntimeError("prepared frontend cleanup failed")


async def _acquire_settings_restart(
    user: dict | None,
    mode: str,
    acquired_ids: list[str],
    *,
    trigger: str | None = None,
    target_version: str | None = None,
) -> str:
    """Acquire one Settings restart while retaining a committed ID on cancellation."""
    from automation import self_restart
    from infra.db_async import run_in_db
    from infra.update_status import cached_changes, cached_target_version

    def _try_acquire() -> str | None:
        operation_id = self_restart.try_acquire(
            None,
            user.get("id") if user else None,
            None,
            None,
            mode,
            kind="update" if mode == "update" else "restart",
            trigger=trigger if mode == "update" else None,
            target_version=(target_version or cached_target_version()) if mode == "update" else None,
            changes=json.dumps(cached_changes()) if mode == "update" and cached_changes() else None,
        )
        if operation_id is not None:
            acquired_ids.append(operation_id)
        return operation_id

    operation_id = await run_in_db(_try_acquire)
    if operation_id is None:
        raise HTTPException(409, "A restart is already in flight.")
    return operation_id


async def _begin_settings_drain(operation_id: str, mode: str) -> None:
    """Establish the exact durable drain before handing lifecycle to the shell."""
    from automation.restart import begin_restart_drain

    reason = "update and restart" if mode == "update" else "restart"
    try:
        await begin_restart_drain(
            reason,
            operation_id=operation_id,
            allow_hard_cap=True,
        )
    except Exception as exc:
        raise HTTPException(
            409, f"Restart control could not be established: {exc}"
        ) from exc


async def _compensate_cancelled_acquire(
    acquired_ids: list[str],
    cancellation: asyncio.CancelledError,
) -> None:
    """Compensate committed ownership without replacing the caller cancellation."""
    if acquired_ids:
        from automation.restart import abort_restart_control

        try:
            await abort_restart_control(
                acquired_ids[0],
                error="settings restart cancelled before launch",
                cancelled=True,
                failure_code="drain_cancelled",
            )
        except Exception as error:
            cancellation.add_note(f"Restart cancellation compensation failed: {error}")
        except asyncio.CancelledError:
            cancellation.add_note("Restart cancellation compensation was interrupted.")


async def _compensate_preparation_failure(
    operation_id: str,
    failure: BaseException,
    *,
    failure_code: str | None = None,
    failure_detail: str | None = None,
) -> None:
    """Release the exact reservation without replacing its preparation failure."""
    from automation.restart import abort_restart_control

    try:
        released = await abort_restart_control(
            operation_id,
            error=_failure_summary(failure),
            failure_code=failure_code,
            failure_detail=failure_detail,
        )
    except asyncio.CancelledError:
        failure.add_note("Restart preparation compensation was interrupted.")
    except Exception as error:
        failure.add_note(f"Restart preparation compensation failed: {error}")
    else:
        if not released:
            failure.add_note("Restart preparation compensation lost exact ownership.")


async def _await_preparation(task: asyncio.Task[None]) -> None:
    """Await one owned preparation worker before propagating caller cancellation."""
    pending_cancel: asyncio.CancelledError | None = None
    while not task.done():
        try:
            await asyncio.shield(task)
        except asyncio.CancelledError as error:
            pending_cancel = pending_cancel or error
        except BaseException:
            break
    if pending_cancel is not None:
        if not task.cancelled():
            task.exception()
        raise pending_cancel
    task.result()


async def _prepare_owned_release(
    operation_id: str,
    acquired_ids: list[str],
    corsia_script: Path,
    restart_env: dict[str, str],
) -> None:
    """Keep exact restart ownership until preparation settles or is compensated."""
    task = asyncio.create_task(
        asyncio.to_thread(_prepare_current_release, corsia_script, restart_env)
    )
    try:
        await _await_preparation(task)
    except asyncio.CancelledError as error:
        await _compensate_cancelled_acquire(acquired_ids, error)
        raise
    except BaseException as error:
        await _compensate_preparation_failure(
            operation_id,
            error,
            failure_code=getattr(error, "update_failure_code", None),
            failure_detail=_failure_detail(error),
        )
        raise


async def _check_room_or_compensate(operation_id: str, acquired_ids: list[str]) -> None:
    """Refuse an update without backup room, releasing the hold on any failure."""
    try:
        await _check_update_backup_room()
    except asyncio.CancelledError as error:
        await _compensate_cancelled_acquire(acquired_ids, error)
        raise
    except BaseException as error:
        from infra.database_migrations import BackupRoomError

        room = {"needed_bytes": error.needed_bytes, "free_bytes": error.free_bytes} if isinstance(error, BackupRoomError) else None
        await _compensate_preparation_failure(
            operation_id,
            error,
            failure_code="disk_space" if room else getattr(error, "update_failure_code", None),
            failure_detail=json.dumps(room) if room else _failure_detail(error),
        )
        if room:
            raise HTTPException(409, {"code": "disk_space", **room}) from error
        raise


async def _launch_settings_restart(
    operation_id: str,
    prepared: bool,
    corsia_script: Path,
    restart_env: dict[str, str],
    script_args: list[str],
    restart_log_path: Callable[[str], Path],
    *,
    caller_token: str | Callable[[str], str] | None = None,
) -> object:
    """Launch the detached lifecycle process or compensate its failed handoff."""
    log_path = restart_log_path("restart.log")
    try:
        with log_path.open("w") as log_fh:
            return spawn_restart_lifecycle(
                corsia_script,
                script_args,
                restart_env,
                operation_id,
                log_fh,
                caller_credential=(
                    caller_token(operation_id)
                    if callable(caller_token)
                    else caller_token
                ),
            )
    except (OSError, ValueError) as exc:
        from automation.restart import abort_restart_control

        release_failed = False
        try:
            released = await abort_restart_control(
                operation_id,
                error="settings restart launch failed",
                failure_code="launch_failed",
                failure_detail=json.dumps({"log_path": str(log_path)}),
            )
        except Exception:
            release_failed = True
        else:
            release_failed = not released
        detail = (
            "Update completed, but safe restart could not be started; "
            "CORSIA is still running."
            if prepared
            else "Safe restart could not be started; CORSIA is still running."
        )
        if release_failed:
            detail += f" {_RELEASE_FAILURE_NOTE}"
        raise HTTPException(500, detail) from exc


async def prelaunch_settings_restart(
    user: dict | None,
    mode: str,
    corsia_script: Path,
    restart_env: dict[str, str],
    script_args: list[str],
    restart_log_path: Callable[[str], Path],
    *,
    caller_token: str | Callable[[str], str],
    trigger: str | None = "manual",
    target_version: str | None = None,
) -> tuple[str, object]:
    """Hand Settings restart ownership from preparation through child launch."""
    prepared = mode == "update"
    if prepared:
        await _UPDATE_REQUEST_LOCK.acquire()
    try:
        acquired_ids: list[str] = []
        try:
            operation_id = await _acquire_settings_restart(
                user,
                mode,
                acquired_ids,
                trigger=trigger,
                target_version=target_version,
            )
        except asyncio.CancelledError as error:
            await _compensate_cancelled_acquire(
                acquired_ids,
                error,
            )
            raise

        if prepared:
            await _check_room_or_compensate(operation_id, acquired_ids)
            await _prepare_owned_release(
                operation_id, acquired_ids, corsia_script, restart_env
            )
            script_args = ["restart-prepared"]

        try:
            await _begin_settings_drain(operation_id, mode)
        except asyncio.CancelledError as error:
            await _compensate_cancelled_acquire(
                acquired_ids,
                error,
            )
            raise

        restart_env["CORSIA_RESTART_OPERATION_ID"] = operation_id
        process = await _launch_settings_restart(
            operation_id,
            prepared,
            corsia_script,
            restart_env,
            script_args,
            restart_log_path,
            caller_token=caller_token,
        )
        return operation_id, process
    finally:
        if prepared:
            _UPDATE_REQUEST_LOCK.release()
