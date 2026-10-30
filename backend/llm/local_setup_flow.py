"""Orchestrate setup and Settings work through the one flight owner."""

from __future__ import annotations

import asyncio
from typing import Callable

from infra.failure_reference import new_reference
from llm import local_setup as ls, local_setup_operation, local_setup_prepare as prepare
from llm.local_keep_loaded import keep_loaded
from llm.local_model import LocalModel, record_set_up
from llm.local_profiles import format_size
from llm.local_setup_disk import DiskSpaceError
from llm.local_setup_download import StillDownloading
from llm.local_setup_gates import local_memory_ok, limited_subscription_message, subscription_available
from llm.local_setup_results import LOAD_FAILURE_CAUSES, LoadFailed, LoadRefused, SETUP_COMMAND, failure_cause, failure_message, local_setup_error
from llm.local_setup_runtime import apply_optimal_settings, configure_model_settings, start_managed_omlx


async def _check_load(server_id: str, model: LocalModel) -> None:
    try:
        outcome = await keep_loaded(server_id, model, force_pin=True)
    except Exception as exc:
        raise LoadFailed("Model load failed") from exc
    if outcome["status"] == "refused":
        neighbour = None
        if outcome.get("neighbour_id"):
            from infra.db_async import run_in_db
            from llm.catalog import get_model_by_litellm

            identity = f"omlx@{server_id}/{outcome['neighbour_id']}"
            try:
                row = await run_in_db(get_model_by_litellm, identity)
                if row and row.get("litellm_model") == identity:
                    neighbour = row.get("display_name")
            except Exception:
                ls.log.warning("Could not look up the loaded model's display name", exc_info=True)
        raise LoadRefused(outcome["verdict"], outcome.get("line"), neighbour)
    if outcome["status"] == "failed":
        raise LoadFailed(outcome.get("detail") or "Model load failed") from outcome.get("exception")


async def _setup_new_omlx(report: Callable[..., None], events: list[str],
                         model: LocalModel | None = None, *, retry_after_load_failure: bool = False) -> dict:
    from llm.local_sources import local_server_for_install

    await prepare.join()
    resolved = await local_server_for_install(report, start_managed=start_managed_omlx)
    assert resolved is not None
    server_id, port, existing = resolved
    if existing and not retry_after_load_failure:
        return {"ok": True, "existing": True, "server_id": server_id,
                "message": "A local model is already connected. Its settings are unchanged.", "progress": events}
    if retry_after_load_failure:
        from llm.local_keep_loaded import _model_rows
        from llm.local_model import set_up_model

        recorded = await asyncio.to_thread(set_up_model)
        if not recorded or recorded != model:
            return {"state": "stopped", "ok": False, "progress": events}
        rows = await _model_rows(server_id)
        if rows is None:
            raise LoadFailed("oMLX model list unavailable")
        if not any(row.get("id") == recorded.model for row in rows):
            return {"state": "stopped", "ok": False, "progress": events}
        ls._record_model(recorded)
        ls.record_downloaded()
        model = recorded
    model = model or prepare.picked_model()
    if model is None:
        report("Finding the model that fits this computer")
        model = await prepare.ask(server_id, port)
    if not retry_after_load_failure:
        ls._record_model(model)
        await ls._download(server_id, report, model)
        ls.record_downloaded()
        # Same order as a Settings download: apply the model's settings first.
        optimal = await apply_optimal_settings(server_id, model)
        if optimal.status != "applied" and not optimal.writer_attempted:
            await configure_model_settings(server_id, model)
        report("Refreshing model catalog", finishing=True)
        from llm.catalog_network_models import discover_network_models

        result = await discover_network_models(server_id)
        if not result.get("ok"):
            raise RuntimeError(result.get("error") or "The local model catalog could not refresh.")
        await asyncio.to_thread(record_set_up, model)
    report("Loading model", finishing=True)
    await _check_load(server_id, model)
    ls._emit("completed", model=model.repository)
    return {"ok": True, "server_id": server_id, "model": model.model,
            "download_size": format_size(model.download_size_bytes), "progress": events}


async def _run_flight(*, model: LocalModel | None = None, retry_after_load_failure: bool = False,
                      on_stage: Callable[[str], None] | None = None) -> dict:
    events: list[str] = []

    def report(stage: str, *, finishing: bool = False) -> None:
        ls.report_stage(stage, finishing=finishing)
        events.append(stage)
        if on_stage is not None:
            on_stage(stage)

    try:
        result = await _setup_new_omlx(report, events, model, retry_after_load_failure=retry_after_load_failure)
    except asyncio.CancelledError:
        ls._release_operation_lock()
        raise
    except StillDownloading:
        result = {**ls._stopped_following(ls.selected_model(), events),
                  "next_action": f"Run `{SETUP_COMMAND}` to follow it again."}
    except Exception as exc:
        cause, ref = failure_cause(exc), new_reference("local")
        ls.log.exception("Local setup failed reference=%s cause=%s", ref, cause)
        result = {**local_setup_error(failure_message(exc)), "progress": events,
                  "cause": cause, "ref": ref, "pre_download": ls.selected_model() is None}
        if isinstance(exc, LoadRefused):
            result.update(verdict=exc.verdict, catalog_line=exc.line, neighbour=exc.neighbour)
        if isinstance(exc, DiskSpaceError):
            result["bytes_to_free"] = exc.bytes_to_free
        ls._emit("failed", cause=cause, ref=ref)
    ls.finish_flight(result)
    await ls._notify_terminal_setup_download(result)
    return result


async def _resume(operation: local_setup_operation.Operation) -> dict:
    await ls.wait_for_operation()
    if operation.kind == "settings":
        from llm.local_settings_download import _run_flight as settings_flight

        if operation.model is None:
            raise ValueError("a Settings download needs its selected model")
        return await settings_flight(operation.model)
    return await _run_flight(model=operation.model)


async def resume_pending_setup() -> bool:
    """Re-follow a persisted task after restart without issuing another download."""
    if ls._running():
        return True
    operation = local_setup_operation.read()
    if operation is None:
        return False
    if not operation.in_flight:
        ls.restore_terminal_flight(operation)
        return False
    if operation.kind not in {"setup", "settings"} or not await asyncio.to_thread(local_memory_ok):
        ls.log.warning("discarding local setup recovery record: invalid kind or insufficient memory")
        local_setup_operation.clear()
        return False
    ls.start_flight(_resume(operation), "Waiting to resume local model setup", operation=operation)
    ls._emit("resumed", model=operation.repository, kind=operation.kind)
    return True


def _in_progress_or_blocked() -> dict:
    """The running flight, or the reason none could start: a press never ends silently with nothing running."""
    if ls._running():
        return ls._in_progress_result()
    ref = new_reference("local")
    ls.log.warning("Local setup could not claim its operation reference=%s", ref)
    return {**local_setup_error("CORSIA couldn't start the download."), "reason": "operation_held", "ref": ref,
            "fix": "Quit CORSIA completely, reopen it, then press Retry."}


async def begin_local_setup(*, no_subscription_only: bool = False,
                            automatic: bool = False, onboarding: bool = False) -> dict:
    """Start setup, or report its existing operation and eligibility decision."""
    if ls._running():
        return ls._in_progress_result()
    operation = local_setup_operation.read()
    if operation is not None and operation.in_flight:
        if await resume_pending_setup():
            return ls._in_progress_result()
        return local_setup_error("CORSIA could not resume the local model setup safely.")
    from llm.local_sources import local_server_for_install

    existing = await local_server_for_install(lambda _stage: None, start_if_absent=False)
    if existing is not None:
        server_id, _port, _is_existing = existing
        refused = operation is not None and operation.downloaded and (operation.result or {}).get("cause") in LOAD_FAILURE_CAUSES
        if not ls._claim_operation("setup", onboarding=onboarding):
            return _in_progress_or_blocked()
        if refused:
            ls.start_flight(_run_flight(model=operation.model, retry_after_load_failure=True), "Installing")
            ls._emit("started")
            return ls._in_progress_result()
        return ls.finish_flight({"ok": True, "existing": True, "server_id": server_id,
                                 "message": "A local model is already connected. Its settings are unchanged."})
    memory_ok = await asyncio.to_thread(local_memory_ok)
    ls.clear_result()
    subscription = await subscription_available() if no_subscription_only else None
    if subscription is not None:
        source = "Claude Code" if subscription.provider == "claude-code" else "Codex"
        account = f" as {subscription.label}" if subscription.label else ""
        return ls._skipped("subscription_available",
                           f"{source} is signed in{account}, so the local model is skipped. You can add one from the app.")
    notice = await limited_subscription_message() if no_subscription_only else None
    if not memory_ok:
        message = "This computer can't run a local model well. Connect a subscription or an API key in the app."
        if notice:
            return ls._skipped("ineligible", f"{notice}\nThis computer can't run a local model well. Connect another subscription or an API key in the app.")
        if automatic:
            return ls._skipped("ineligible", message)
        ls._emit("skipped", reason="ineligible")
        return {**local_setup_error(message), "progress": []}
    if ls._running():
        return ls._in_progress_result()
    if not ls._claim_operation("setup", onboarding=onboarding):
        await resume_pending_setup()
        return _in_progress_or_blocked()
    ls.start_flight(_run_flight(), "Installing")
    ls._emit("started")
    return ls._in_progress_result(notice=notice)


async def begin_settings_download(model: LocalModel) -> dict:
    """Start Settings work under the same exclusive lifecycle as setup."""
    from llm.local_settings_download import _run_flight as settings_flight

    if ls._running():
        return ls._in_progress_result()
    operation = local_setup_operation.read()
    if operation is not None and operation.in_flight:
        if await resume_pending_setup():
            return ls._in_progress_result()
        return {"ok": False, "reason": "recovery_failed", "message": "Couldn't resume the local model download. Try again."}
    if not await asyncio.to_thread(local_memory_ok):
        return {"ok": False, "reason": "mac_cannot_run", "message": "This computer can't run a local model well."}
    if not ls._claim_operation("settings", model):
        await resume_pending_setup()
        return _in_progress_or_blocked()
    ls.start_flight(settings_flight(model), "Preparing download")
    ls._emit("started", model=model.repository, kind="settings")
    return ls._in_progress_result()
