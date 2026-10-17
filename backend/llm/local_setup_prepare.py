"""Prepare: CORSIA's managed oMLX picks the local model while Welcome is open.

Welcome sizes the local model before anything downloads, but on a first install
the managed oMLX that picks it does not exist yet. So prepare installs and
starts that oMLX the same way setup does, then asks its recommender
(``local_model``). The setup flight joins a running prepare and takes its pick,
or asks itself; it never downloads without a pick. One in-process record holds
the state, so a server restart returns it to not checked.

The flight state and the oMLX calls live in ``local_setup``; they are read
through the module at call time so one set of names is patched and observed.
"""

from __future__ import annotations

import asyncio
import logging
import subprocess
from dataclasses import dataclass, field

from llm import local_model
from llm.local_model import LocalModel, PickUnavailable
from llm.local_profiles import OWN_OMLX_STARTER
from llm.local_setup_results import failure_message

log = logging.getLogger(__name__)

#: Under 24 GiB: no local model, so Welcome shows no switch.
LOW_MEMORY = "low_memory"
#: An oMLX CORSIA doesn't manage is here; prepare never starts a second one.
OWN_OMLX = "own_omlx"
NOT_CHECKED = "not_checked"
CHECKING = "checking"
PICKED = "picked"
#: The recommender could not answer, even once more after oMLX reported ready.
FAILED = "failed"

#: Under 24 GiB there is no local model, so the one message says to connect something else.
NO_LOCAL_MODEL = "This computer can't run a local model well. Connect a subscription or an API key in the app."


def fallback_note(model: LocalModel) -> str:
    """Why this computer got the starter model: oMLX's recommender had no row to offer. The name is the profile's."""
    return f"oMLX didn’t recommend a model for this computer, so CORSIA chose {model.display_name}."


@dataclass
class _Prepare:
    task: asyncio.Task | None = None
    state: str = NOT_CHECKED
    model: LocalModel | None = None
    message: str | None = None
    # The pick is the starter model because the recommender had no usable row.
    fallback: bool = False
    # Whether prepare started the oMLX it set up, so turning the switch off stops it.
    started_omlx: bool = False
    # Requests waiting for the pick to leave checking; ``_settle`` wakes them.
    waiters: list[asyncio.Future] = field(default_factory=list)


_prepare = _Prepare()
_prepare_lock = asyncio.Lock()


def _running() -> bool:
    return _prepare.task is not None and not _prepare.task.done()


def _settle(state: str, model: LocalModel | None = None, message: str | None = None, fallback: bool = False) -> None:
    _prepare.state, _prepare.model, _prepare.message, _prepare.fallback = state, model, message, fallback
    if state != CHECKING:
        while _prepare.waiters:
            waiter = _prepare.waiters.pop()
            if not waiter.done():
                waiter.set_result(None)


def _view(state: str, model: LocalModel | None = None, message: str | None = None) -> dict:
    if state == LOW_MEMORY:
        message = NO_LOCAL_MODEL
    elif state == PICKED and model is not None and _prepare.fallback and model == OWN_OMLX_STARTER:
        message = fallback_note(model)
    return {"state": state, "model": model.view() if model is not None else None, "message": message}


def picked_model() -> LocalModel | None:
    """The model prepare picked in this server process, else the one a finished setup installed.

    A finished setup's model is this Mac's own record, not a shipped pick, so
    running setup again keeps it rather than asking for another.
    """
    if _prepare.state == PICKED:
        return _prepare.model
    return local_model.set_up_model() if _prepare.state == NOT_CHECKED else None


def known_model() -> LocalModel | None:
    """The local model as setup knows it now: the running flight's, else ``picked_model``."""
    from llm import local_setup as ls

    return ls.flight_model() or picked_model()


def prepare_task() -> asyncio.Task | None:
    """Prepare's task for lifespan shutdown to cancel and join, else None."""
    return _prepare.task


async def pick_view() -> dict:
    """What Welcome's local row shows: the state, the picked model, and why there is none."""
    from llm import local_setup as ls

    if not await asyncio.to_thread(ls.local_memory_ok):
        return _view(LOW_MEMORY)
    flying = ls.flight_model()
    if flying is not None:
        return _view(PICKED, flying)
    if _running():
        return _view(CHECKING)
    if _prepare.state != NOT_CHECKED:
        return _view(_prepare.state, _prepare.model, _prepare.message)
    # Read only before prepare touches oMLX: mid-install, CORSIA's own oMLX is not marked yet.
    if await asyncio.to_thread(ls.existing_install_is_foreign):
        return _view(OWN_OMLX)
    set_up = await asyncio.to_thread(local_model.set_up_model)
    return _view(PICKED, set_up) if set_up is not None else _view(NOT_CHECKED)


async def begin_prepare() -> dict:
    """Start the pick on Welcome, or again after it failed; return what Welcome shows.

    A setup flight already running owns oMLX and the pick, so this then only reports.
    """
    from llm import local_setup as ls

    async with _prepare_lock:
        view = await pick_view()
        if view["state"] not in {NOT_CHECKED, FAILED} or ls._running():
            return view
        _settle(CHECKING)
        _prepare.task = asyncio.create_task(_run())
        _prepare.task.add_done_callback(_observe)
        return _view(CHECKING)


async def settled_view() -> dict:
    """``pick_view`` once the pick is no longer checking.

    No event reaches Welcome before the first account exists, so its request
    waits here for the answer instead of polling. Every way out of checking
    goes through ``_settle``, which wakes it, and the task that settled has
    finished by the time the waiter runs.
    """
    view = await pick_view()
    while view["state"] == CHECKING:
        waiter = asyncio.get_running_loop().create_future()
        _prepare.waiters.append(waiter)
        try:
            await waiter
        finally:
            if waiter in _prepare.waiters:
                _prepare.waiters.remove(waiter)
        view = await pick_view()
    return view


async def stop_prepare() -> dict:
    """The local switch went off: stop the pick and the oMLX prepare started.

    Prepare downloads nothing, so nothing else needs undoing. A setup flight
    already running owns oMLX, so this leaves it alone.
    """
    from llm import local_setup as ls

    if ls._running():
        return await pick_view()
    task = _prepare.task
    if task is not None and not task.done():
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)
    if _prepare.started_omlx:
        try:
            await asyncio.to_thread(ls._run, ["omlx", "stop"])
        except (OSError, subprocess.SubprocessError):
            log.warning("Could not stop the oMLX that local model prepare started", exc_info=True)
    _prepare.task, _prepare.started_omlx = None, False
    _settle(NOT_CHECKED)
    return await pick_view()


async def _run() -> None:
    from llm import local_setup as ls
    from llm.local_setup_new_omlx import start_managed_omlx

    try:
        port = await asyncio.to_thread(ls.own_omlx_port)
        # An earlier try may have started it; an oMLX that already answered is not prepare's to stop.
        _prepare.started_omlx = _prepare.started_omlx or not await ls._omlx_answers(port)
        server_id, port = await start_managed_omlx(lambda _stage: None)
        await ask(server_id, port)
    except asyncio.CancelledError:
        _settle(NOT_CHECKED)
        raise
    except Exception as exc:
        if _prepare.state == CHECKING:
            log.warning("Local model prepare failed: %s", exc)
            _settle(FAILED, message=failure_message(exc))


async def join() -> None:
    """Wait for a running prepare, without cancelling it if the waiter is cancelled."""
    task = _prepare.task
    if task is not None and not task.done():
        await asyncio.wait({task})


async def ask(server_id: str, port: int) -> LocalModel:
    """Ask the recommender, once more after oMLX reports ready; record and return the pick.

    When it answers with no usable row, the pick is the starter model from
    ``local_profiles.json`` (the one list CORSIA keeps) and the view says so.
    ``PickUnavailable`` (or oMLX's health failure) means it could not answer.
    """
    from llm import local_setup as ls

    _settle(CHECKING)
    try:
        try:
            model = await local_model.ask_recommender(server_id)
        except PickUnavailable as first:
            log.info("oMLX recommender unavailable (%s); asking once more when oMLX is ready", first)
            await ls._wait_for_health(port)
            model = await local_model.ask_recommender(server_id)
    except asyncio.CancelledError:
        _settle(NOT_CHECKED)
        raise
    except Exception as exc:
        _settle(FAILED, message=failure_message(exc))
        raise
    fallback = model is None
    model = model or OWN_OMLX_STARTER
    _settle(PICKED, model, fallback=fallback)
    return model


def _observe(task: asyncio.Task) -> None:
    if not task.cancelled() and task.exception() is not None:
        log.error("Local model prepare ended with an error", exc_info=task.exception())
