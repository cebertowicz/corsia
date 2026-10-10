"""Settings restart preparation, durable acquisition, and child launch handoff."""

from __future__ import annotations

import asyncio
import re
import subprocess
import uuid
from collections.abc import Callable
from pathlib import Path

from fastapi import HTTPException

from api.restart_shell import spawn_restart_lifecycle

_UPDATE_REQUEST_LOCK = asyncio.Lock()
_RELEASE_FAILURE_NOTE = (
    "Restart control could not be released automatically; "
    "the active operation may still be holding ingress."
)



def _update_log_path() -> Path:
    """Return an immutable per-attempt log path for one Update request."""
    from infra.config import LOGS_DIR

    return LOGS_DIR / f"update-{uuid.uuid4().hex}.log"


def _clean_update_output(value: str | bytes | None) -> str:
    if not value:
        return ""
    if isinstance(value, bytes):
        value = value.decode(errors="replace")
    return re.sub(r"\x1b\[[0-9;]*m", "", value)


def _terminal_update_summary(
    stdout: str | bytes | None, stderr: str | bytes | None
) -> str:
    """Project noisy command output to one bounded terminal failure."""
    clean_stdout = _clean_update_output(stdout)
    clean_stderr = _clean_update_output(stderr)
    stderr_lines = [line.strip() for line in clean_stderr.splitlines() if line.strip()]
    stdout_lines = [line.strip() for line in clean_stdout.splitlines() if line.strip()]

    for line in stderr_lines:
        if re.match(r"^(?:error|fatal):", line, re.IGNORECASE):
            return line[:300]
    for line in reversed(stdout_lines):
        if line.startswith("❌") or "Redeploy refused:" in line:
            return line.removeprefix("❌").strip()[:300]
    fallback = stderr_lines[-1:] or stdout_lines[-1:]
    if fallback:
        return fallback[0][:300]
    return "the pull, dependency check, or frontend build failed"


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
        raise HTTPException(
            504, "Update timed out before restart; CORSIA is still running."
        ) from exc
    except OSError as exc:
        raise HTTPException(500, f"Could not start the update: {exc}") from exc

    stdout = _clean_update_output(result.stdout)
    stderr = _clean_update_output(result.stderr)
    log_path = _update_log_path()
    try:
        log_path.parent.mkdir(parents=True, exist_ok=True)
        log_path.write_text(
            "\n".join(part.rstrip() for part in (stdout, stderr) if part.strip()) + "\n"
        )
    except OSError as exc:
        if result.returncode != 0:
            raise HTTPException(
                500,
                "Update stopped before restart, and its full log could not be written; "
                "CORSIA is still running.",
            ) from exc
    if result.returncode == 0:
        return

    summary = _terminal_update_summary(stdout, stderr)
    raise HTTPException(
        409,
        f"Update stopped before restart: {summary}. Full log: {log_path}",
    )


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
    user: dict,
    mode: str,
    acquired_ids: list[str],
) -> str:
    """Acquire one Settings restart while retaining a committed ID on cancellation."""
    from automation import self_restart
    from infra.db_async import run_in_db

    def _try_acquire() -> str | None:
        operation_id = self_restart.try_acquire(
            None,
            user.get("id"),
            None,
            None,
            mode,
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
            )
        except Exception as error:
            cancellation.add_note(f"Restart cancellation compensation failed: {error}")
        except asyncio.CancelledError:
            cancellation.add_note("Restart cancellation compensation was interrupted.")


async def _compensate_preparation_failure(
    operation_id: str,
    failure: BaseException,
) -> None:
    """Release the exact reservation without replacing its preparation failure."""
    from automation.restart import abort_restart_control

    try:
        released = await abort_restart_control(
            operation_id,
            error="settings restart preparation failed",
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
        await _compensate_preparation_failure(operation_id, error)
        raise


async def _launch_settings_restart(
    operation_id: str,
    prepared: bool,
    corsia_script: Path,
    restart_env: dict[str, str],
    script_args: list[str],
    restart_log_path: Callable[[str], Path],
    *,
    caller_token: str | None = None,
) -> object:
    """Launch the detached lifecycle process or compensate its failed handoff."""
    try:
        with restart_log_path("restart.log").open("w") as log_fh:
            return spawn_restart_lifecycle(
                corsia_script,
                script_args,
                restart_env,
                operation_id,
                log_fh,
                caller_credential=caller_token,
            )
    except (OSError, ValueError) as exc:
        from automation.restart import abort_restart_control

        release_failed = False
        try:
            released = await abort_restart_control(
                operation_id,
                error="settings restart launch failed",
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
    user: dict,
    mode: str,
    corsia_script: Path,
    restart_env: dict[str, str],
    script_args: list[str],
    restart_log_path: Callable[[str], Path],
    *,
    caller_token: str,
) -> tuple[str, object]:
    """Hand Settings restart ownership from preparation through child launch."""
    prepared = mode == "update"
    if prepared:
        await _UPDATE_REQUEST_LOCK.acquire()
    try:
        acquired_ids: list[str] = []
        try:
            operation_id = await _acquire_settings_restart(user, mode, acquired_ids)
        except asyncio.CancelledError as error:
            await _compensate_cancelled_acquire(
                acquired_ids,
                error,
            )
            raise

        if prepared:
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
