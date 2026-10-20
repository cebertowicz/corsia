"""Carry a v0.3/v0.4 install's local models over to CORSIA's managed oMLX.

Before v0.4.1 local setup ran oMLX as a Homebrew service on the person's
``~/.omlx``. Once that service is proven CORSIA's own (``local_setup_ownership``),
startup stops it, moves only the model folders CORSIA's own records say CORSIA
downloaded into the managed model directory, starts the managed server and lets
the old row retire. Nothing else under the old oMLX root is moved, deleted or
rewritten.

A move is a same-volume rename when possible; otherwise a copy that is checked
by file count and size before the source is removed. Every step can run again: a
finished move is recognised, an interrupted copy is discarded and redone, and a
managed-start failure stops CORSIA's managed server before it attempts to return
recorded models to the earlier service. Setup records and other old oMLX files
stay unchanged. Startup never waits on this: it runs as one background task.
"""

from __future__ import annotations

import asyncio
import errno
import json
import logging
import os
import shutil
from dataclasses import dataclass
from pathlib import Path

from llm.local_model import LocalModel

log = logging.getLogger(__name__)

_DONE_NAME = "legacy-omlx-upgraded"
_task: asyncio.Task | None = None


@dataclass(frozen=True)
class Moved:
    """One recorded model's outcome: ``moved``, ``already``, ``absent`` or ``failed``."""

    repository: str
    outcome: str


def done_path() -> Path:
    from infra.config import STATE_ROOT

    return Path(STATE_ROOT) / _DONE_NAME


def legacy_model_dir() -> Path:
    """The model directory CORSIA's old Homebrew oMLX downloaded into."""
    from llm.local_setup_ownership import legacy_omlx_settings_file

    settings = legacy_omlx_settings_file()
    try:
        raw = json.loads(settings.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        raw = {}
    model = raw.get("model") if isinstance(raw, dict) else None
    configured = model.get("model_dir") if isinstance(model, dict) else None
    if isinstance(configured, str) and configured:
        return Path(configured).expanduser()
    return settings.parent / "models"


#: oMLX's own default port, used when the old settings file names none.
_OMLX_DEFAULT_PORT = 8000


def legacy_omlx_port() -> int:
    """Read the prior CORSIA service port without consulting live upgrade state."""
    from llm.local_setup_ownership import legacy_omlx_settings_file

    try:
        raw = json.loads(legacy_omlx_settings_file().read_text(encoding="utf-8"))
    except (OSError, ValueError):
        raw = {}
    server = raw.get("server") if isinstance(raw, dict) else None
    port = server.get("port") if isinstance(server, dict) else None
    return port if isinstance(port, int) and not isinstance(port, bool) else _OMLX_DEFAULT_PORT


def pending_legacy_port() -> int | None:
    """The port CORSIA's old service serves on while its upgrade has not finished, else None.

    Until then its row is the person's only route to their local model, so
    startup keeps it; a successful upgrade retires it when the managed server
    registers.
    """
    from llm.local_setup_ownership import legacy_omlx_settings_file, legacy_setup_owns_current_omlx

    if done_path().exists() or not legacy_setup_owns_current_omlx():
        return None
    return legacy_omlx_port()


def recorded_models(legacy_dir: Path) -> list[LocalModel]:
    """The models CORSIA's own records say CORSIA downloaded.

    Setup's record, else the starter a setup finished before that record
    existed (proven complete in the old directory), plus each finished Settings
    download. Nothing else in the old directory is CORSIA's.
    """
    from llm import local_downloads
    from llm.local_model import set_up_model
    from llm.local_profiles import STARTER_MODEL
    from llm.local_setup_disk import download_complete
    from llm.local_setup_managed import managed_omlx_paths

    models: list[LocalModel] = []
    setup = set_up_model()
    if setup is not None:
        models.append(setup)
    elif any(download_complete(folder / STARTER_MODEL.repository, STARTER_MODEL.download_size_bytes)
             for folder in (legacy_dir, managed_omlx_paths().model_dir)):
        models.append(STARTER_MODEL)
    models += [entry.model for entry in local_downloads.all() if entry.status == "available"]
    unique: dict[str, LocalModel] = {}
    for model in models:
        unique.setdefault(model.repository, model)
    return list(unique.values())


def _tree(folder: Path) -> tuple[int, int]:
    """File count and total bytes under ``folder``, links counted, not followed."""
    files = size = 0
    for root, _dirs, names in os.walk(folder):
        for name in names:
            files += 1
            size += (Path(root) / name).lstat().st_size
    return files, size


def move_model_folder(source: Path, target: Path) -> str:
    """Move one model folder; return ``moved``, ``already``, ``absent`` or ``failed``.

    The source is removed only once an identical tree is in place. An earlier
    run's leftovers are finished or discarded, never trusted blindly.
    """
    staging = target.with_name(f".{target.name}.moving")
    try:
        if target.exists():
            if not source.exists():
                return "already"
            # An earlier run put the copy in place but stopped before removing the source.
            if _tree(source) == _tree(target):
                shutil.rmtree(source)
                return "moved"
            log.warning("kept both copies of a local model: the managed copy differs from the original")
            return "already"
        if not source.is_dir():
            return "absent"
        target.parent.mkdir(parents=True, exist_ok=True)
        try:
            os.rename(source, target)
            return "moved"
        except OSError as error:
            if error.errno != errno.EXDEV:
                raise
        # Another volume: copy beside the target, check it, then swap it in.
        if staging.exists():
            shutil.rmtree(staging)
        expected = _tree(source)
        shutil.copytree(source, staging, symlinks=True)
        if _tree(staging) != expected:
            raise OSError("the copied model does not match the original")
        os.rename(staging, target)
    except OSError as error:
        log.warning("could not move a local model to CORSIA's model directory: %s", error)
        shutil.rmtree(staging, ignore_errors=True)
        return "failed"
    try:
        shutil.rmtree(source)
    except OSError as error:
        # The managed copy is complete; the next start removes the verified original.
        log.warning("moved a local model but could not remove the original yet: %s", error)
    return "moved"


def move_recorded_models(legacy_dir: Path, models: list[LocalModel]) -> list[Moved]:
    """Move each recorded model from ``legacy_dir`` into the managed model directory."""
    from llm.local_setup_managed import managed_omlx_paths

    target_dir = managed_omlx_paths().model_dir
    return [
        Moved(model.repository, move_model_folder(legacy_dir / model.repository, target_dir / model.repository))
        for model in models
    ]


def restore_recorded_models(legacy_dir: Path, models: list[LocalModel]) -> list[Moved]:
    """Return recorded models to the legacy directory after managed startup fails.

    This also returns models a prior interrupted upgrade completed before it
    could write its done marker. A legacy copy already in place is left alone;
    it is the copy the restarted service will use.
    """
    from llm.local_setup_managed import managed_omlx_paths

    managed_dir = managed_omlx_paths().model_dir
    return [
        Moved(model.repository, move_model_folder(managed_dir / model.repository,
                                                  legacy_dir / model.repository))
        for model in models
    ]


def _stop_legacy_service() -> bool:
    """Stop CORSIA's old service; True once its launchd job no longer runs."""
    from llm.local_setup_managed import BREW_SERVICE_LABEL, launchd_jobs
    from llm.local_setup_ownership import retire_legacy_owned_omlx_service

    retire_legacy_owned_omlx_service()
    # Judge by what runs, so a service an earlier start stopped counts as stopped
    # and an unreadable job list never counts as stopped.
    jobs = launchd_jobs()
    return jobs is not None and BREW_SERVICE_LABEL not in jobs.values()


def _restart_legacy_service() -> None:
    """Start CORSIA's old service again after a move that moved nothing."""
    import subprocess

    from llm.local_setup_ownership import OMLX_FORMULA, run_command

    brew = shutil.which("brew")
    if not brew:
        log.warning("could not restart CORSIA's earlier oMLX service: Homebrew is missing")
        return
    try:
        run_command([brew, "services", "start", OMLX_FORMULA])
    except (OSError, subprocess.SubprocessError) as error:
        log.warning("could not restart CORSIA's earlier oMLX service: %s", error)


async def _start_managed(setup: LocalModel | None) -> None:
    """Start the managed server on the moved models and list them in the catalog."""
    from llm import local_setup as ls
    from llm.catalog_network_models import discover_network_models
    from llm.local_model import backfill_legacy_record
    from llm.local_setup_new_omlx import start_managed_omlx

    server_id, _port = await start_managed_omlx(lambda stage: log.info("local model upgrade: %s", stage))
    result = await discover_network_models(server_id)
    if not result.get("ok"):
        raise RuntimeError(result.get("error") or "The local model catalog could not refresh.")
    # A setup finished before setup kept a record: record its starter now it is in place.
    model = setup or await asyncio.to_thread(backfill_legacy_record)
    if model is not None:
        await ls._configure_model_settings(server_id, model)


def _managed_server_id() -> str | None:
    from llm import network_discovery
    from llm.local_setup_managed import is_corsia_managed_server

    return next((str(server["id"]) for server in network_discovery.get_servers(provider="omlx")
                 if is_corsia_managed_server(server)), None)


async def _stop_managed_server() -> bool:
    """Stop only CORSIA's canonical managed server before model rollback."""
    from infra.db_async import run_in_db
    from llm.providers.omlx_supervisor import stop_server

    try:
        server_id = await run_in_db(_managed_server_id)
        if server_id is None:
            return True
        result = await stop_server({"server_id": server_id})
    except Exception:
        log.exception("could not stop CORSIA's managed local model server before rollback")
        return False
    if result.get("ok") or result.get("message") == "Not running (no pid file)":
        return True
    log.error("could not stop CORSIA's managed local model server before rollback: %s",
              result.get("message", "unknown error"))
    return False


async def _recover_failed_managed_start(legacy_dir: Path, models: list[LocalModel]) -> None:
    """Stop the managed process, then try to restore and restart the earlier service."""
    try:
        stopped = await _stop_managed_server()
    except Exception:
        log.exception("could not prepare managed local model rollback")
        stopped = False
    if stopped:
        try:
            restored = await asyncio.to_thread(restore_recorded_models, legacy_dir, models)
        except Exception:
            log.exception("could not restore the earlier local model install")
        else:
            not_restored = [item.repository for item in restored if item.outcome == "failed"]
            if not_restored:
                log.error("could not restore local model(s): %s", ", ".join(not_restored))
    else:
        log.error("did not restore models while CORSIA's managed server may still be running")
    try:
        await asyncio.to_thread(_restart_legacy_service)
    except Exception:
        log.exception("could not restart CORSIA's earlier local model service")


def _publish_upgrade_failure() -> None:
    """Expose a managed-start failure through the canonical setup status shape."""
    from api.cli_install import new_reference
    from llm import local_setup
    from llm.local_setup_results import local_setup_error

    ref = new_reference()
    local_setup._flight.result = {
        **local_setup_error("CORSIA couldn't switch to its managed local model server. "
                            "Check the local model status and retry."),
        "cause": "other",
        "ref": ref,
        "pre_download": True,
    }
    log.error("local model upgrade failed after moving models reference=%s", ref)


async def upgrade_legacy_omlx() -> list[Moved] | None:
    """Move a proven v0.3/v0.4 install's models to the managed oMLX; None when there is nothing to do.

    Never raises: a failure is logged, attempts to restore the earlier install,
    and is retried at the next start.
    """
    from llm import local_setup_operation
    from llm.local_model import set_up_model
    from llm.local_setup_ownership import legacy_setup_owns_current_omlx

    try:
        if await asyncio.to_thread(done_path().exists):
            return None
        if not await asyncio.to_thread(legacy_setup_owns_current_omlx):
            return None
        legacy_dir = await asyncio.to_thread(legacy_model_dir)
        models = await asyncio.to_thread(recorded_models, legacy_dir)
        if not models:
            return None
    except Exception:
        log.exception("could not check for an earlier local model install")
        return None
    # The managed oMLX has one owner at a time; a running setup flight retires the service itself.
    lock = await asyncio.to_thread(local_setup_operation.try_lock)
    if lock is None:
        return None
    try:
        if not await asyncio.to_thread(_stop_legacy_service):
            log.warning("CORSIA's earlier oMLX service is still running; its models stay where they are")
            return None
        setup = await asyncio.to_thread(set_up_model)
        moved = await asyncio.to_thread(move_recorded_models, legacy_dir, models)
        failed = [item.repository for item in moved if item.outcome == "failed"]
        if any(item.outcome in {"moved", "already"} for item in moved):
            try:
                await _start_managed(setup)
            except Exception:
                log.exception("could not start the managed local model server; recovering the earlier install")
                await _recover_failed_managed_start(legacy_dir, models)
                _publish_upgrade_failure()
                return None
        else:
            # Nothing reached the managed directory: the old service keeps serving until a later start succeeds.
            await asyncio.to_thread(_restart_legacy_service)
        if failed:
            log.warning("%d local model(s) stayed in the earlier oMLX folder; the next start tries again", len(failed))
        else:
            await asyncio.to_thread(done_path().write_text, "upgraded\n", encoding="utf-8")
            from infra.db_async import run_in_db
            from llm.local_setup_runtime import retire_legacy_setup_omlx_rows

            await run_in_db(retire_legacy_setup_omlx_rows)
        return moved
    except Exception:
        log.exception("could not finish moving the earlier local model install")
        return None
    finally:
        local_setup_operation.unlock(lock)


def start_upgrade() -> asyncio.Task | None:
    """Run ``upgrade_legacy_omlx`` in the background; startup does not wait for it."""
    global _task
    if _task is None or _task.done():
        _task = asyncio.create_task(upgrade_legacy_omlx())
    return _task


def upgrade_task() -> asyncio.Task | None:
    """The upgrade task for lifespan shutdown to cancel and join, else None."""
    return _task
