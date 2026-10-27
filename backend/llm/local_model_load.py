"""Whether a send can use a local oMLX model now, and the one way a send loads it.

A person's send may load a model oMLX has downloaded but not loaded, only when it
fits in the memory free now and never by unloading another model. Nothing else
loads a model: background work (warm-up, cache-warm, checkpoint, summary) asks
oMLX whether the model is loaded and skips when it isn't
(``omlx_lifecycle.require_loaded_model``, ``warm_cache``, ``probe_cache``).

oMLX's ``get_engine`` loads a missing model on a chat request and evicts the
least recently used unpinned model to make room, so "a send never unloads another model" is
enforced here, at load time: ``ensure_loaded`` rechecks the fit under a per-seat
lock and calls oMLX's explicit load, and the chat request is issued only once the
model is loaded. A client outside CORSIA loading into the same oMLX at the same
moment can still cause an eviction; nothing here can see that.

``readiness`` is the cheap synchronous read admission uses; ``ensure_loaded`` is
the awaited load. The verdict on fit comes from ``infra.host_resources`` and from
nowhere else.
"""

from __future__ import annotations

import asyncio
import json
import logging
from dataclasses import dataclass
from functools import partial
from typing import TYPE_CHECKING, Literal

from infra.host_resources import load_verdict, model_room_bytes

if TYPE_CHECKING:
    from llm.resolution import ExecutionTarget

log = logging.getLogger(__name__)

ReadinessState = Literal["loaded", "fits_now", "no_room", "no_memory", "unreachable"]
LoadFailure = Literal["load_failed", "unreachable", "no_room", "no_memory"]


@dataclass(frozen=True, slots=True)
class Readiness:
    """What a send to one model can do now. ``other_loaded`` counts the other models loaded on its seat."""

    state: ReadinessState
    other_loaded: int = 0


class ModelLoadFailed(Exception):
    """A send's load ended without the model loaded; ``cause`` says why (the reason itself is in the log)."""

    def __init__(self, cause: LoadFailure) -> None:
        super().__init__(cause)
        self.cause: LoadFailure = cause


def _seat_and_model(target: ExecutionTarget) -> tuple[str, str] | None:
    """``(server id, model id)`` for a local oMLX target, else None: any other target is never loaded by CORSIA."""
    if not (target.harness == "native" and target.provider == "omlx" and target.backend == "network"):
        return None
    prefix, _, rest = target.transport_model.partition("@")
    server_id, _, model_id = rest.partition("/")
    return (server_id, model_id) if prefix == "omlx" and server_id and model_id else None


def _reported_sizes(db, server_id: str, model_ids: list[str]) -> dict[str, int | None]:
    """The size oMLX reported for each model at discovery, None when it gave none."""
    selectors = {f"omlx@{server_id}/{model_id}": model_id for model_id in model_ids}
    marks = ",".join("?" * len(selectors))
    rows = db.execute(
        f"SELECT litellm_model, metadata_json FROM models WHERE litellm_model IN ({marks})",
        list(selectors),
    ).fetchall()
    sizes: dict[str, int | None] = {model_id: None for model_id in model_ids}
    for row in rows:
        try:
            size = int((json.loads(row["metadata_json"] or "{}")).get("size_bytes") or 0)
        except (TypeError, ValueError, AttributeError):
            log.warning("Unreadable catalog metadata for %s: treating its size as unreported", row["litellm_model"])
            size = 0
        sizes[selectors[row["litellm_model"]]] = size or None
    return sizes


def _on_this_mac(db, server_id: str) -> bool:
    """Whether the seat's server runs on this Mac, read through ``db`` so a claim transaction opens nothing."""
    from llm.locality import server_on_this_mac

    row = db.execute("SELECT id,kind,port,metadata_json FROM network_servers WHERE id=?", (server_id,)).fetchone()
    endpoints = [dict(e) for e in db.execute(
        "SELECT * FROM network_server_endpoints WHERE server_id=?", (server_id,)).fetchall()]
    return row is not None and server_on_this_mac(dict(row), endpoints)


def readiness(target: ExecutionTarget, *, db) -> Readiness:
    """What a send to ``target`` can do now, from cached catalog and sweep state and one host memory read.

    Synchronous, makes no network call and never loads, so a claim transaction may call it.
    A target that isn't a local oMLX model is ``loaded``. ``unreachable``: the server isn't
    online (or isn't known). ``no_room``: it doesn't fit now, and either the free room plus
    the loaded models' sizes would cover it (unloading would help) or a loaded model's size is
    unknown, so it can't be judged. ``no_memory``: it wouldn't fit even with every other model unloaded.
    """
    from llm.network_discovery import get_loaded_model_ids

    seat = _seat_and_model(target)
    if seat is None:
        return Readiness("loaded")
    server_id, model_id = seat
    server = db.execute("SELECT status FROM network_servers WHERE id=?", (server_id,)).fetchone()
    if server is None or server["status"] != "online":
        return Readiness("unreachable")
    loaded = get_loaded_model_ids(server_id)
    others = sorted(loaded - {model_id})
    if model_id in loaded:
        return Readiness("loaded", len(others))
    if not _on_this_mac(db, server_id):
        # The free memory read here is this Mac's, which says nothing about another
        # computer's: its fit can't be judged, so it counts as fitting, like a model with no size.
        return Readiness("fits_now", len(others))
    sizes = _reported_sizes(db, server_id, [model_id, *others])
    verdict = load_verdict(sizes[model_id], [sizes[other] for other in others], model_room_bytes())
    return Readiness(verdict, len(others))


def _readiness_now(target: ExecutionTarget) -> Readiness:
    from infra.database import get_db

    db = get_db()
    try:
        return readiness(target, db=db)
    finally:
        db.close()


# One lock per seat (oMLX server): its loads run one at a time, so each fit check
# sees the memory the previous load took. A reload after a crash takes the same lock.
_SEAT_LOCKS: dict[str, asyncio.Lock] = {}
# The load in flight for a model, shared by every send waiting on it.
_LOADS: dict[tuple[str, str], asyncio.Task[None]] = {}


def seat_lock(server_id: str) -> asyncio.Lock:
    """The lock every load on ``server_id`` holds: ``ensure_loaded``'s, and any other loader's that must not overlap it."""
    return _SEAT_LOCKS.setdefault(server_id, asyncio.Lock())


async def _server_is_up(server_id: str) -> bool:
    from llm.providers.omlx_lifecycle import check_server

    return bool((await check_server({"server_id": server_id})).get("running"))


async def _load(target: ExecutionTarget, server_id: str, model_id: str) -> None:
    """One load, under the seat's lock, rechecking the fit the lock was waited for."""
    from infra.db_async import run_in_db
    from llm.network_discovery import notify_cached_model_loaded
    from llm.providers.omlx_lifecycle import load_model

    async with seat_lock(server_id):
        state = (await run_in_db(_readiness_now, target)).state
        if state == "loaded":
            return
        if state != "fits_now":
            log.info("Not loading %s on %s: %s", model_id, server_id, state)
            raise ModelLoadFailed(state)
        result = await load_model({"server_id": server_id}, model_id)
        if not result.get("ok"):
            up = await _server_is_up(server_id)
            log.warning("oMLX couldn't load %s on %s (%s): %s", model_id, server_id,
                        "server up" if up else "server unreachable", result.get("message"))
            raise ModelLoadFailed("load_failed" if up else "unreachable")
        notify_cached_model_loaded(server_id, model_id)


def _settled(key: tuple[str, str], task: asyncio.Task[None]) -> None:
    """Forget a finished load, and log a failure no caller was left to see (a ``ModelLoadFailed`` is logged where it is raised)."""
    if _LOADS.get(key) is task:
        del _LOADS[key]
    if task.cancelled():
        return
    failure = task.exception()
    if failure is not None and not isinstance(failure, ModelLoadFailed):
        log.error("Loading %s on %s failed unexpectedly", key[1], key[0], exc_info=failure)


async def ensure_loaded(target: ExecutionTarget) -> None:
    """Return once ``target``'s model is loaded, loading it only if it fits now; else raise ``ModelLoadFailed``.

    Concurrent sends for one model share one load. The load is its own task: a caller
    cancelled while waiting (Stop) neither cancels it nor waits for it, and the model ends
    up loaded. Never unloads, and never relies on oMLX's implicit load on a chat request.
    A target that isn't a local oMLX model returns at once.
    """
    from infra.db_async import run_in_db

    seat = _seat_and_model(target)
    if seat is None or (await run_in_db(_readiness_now, target)).state == "loaded":
        return
    task = _LOADS.get(seat)
    if task is None:
        task = asyncio.create_task(_load(target, *seat))
        _LOADS[seat] = task
        task.add_done_callback(partial(_settled, seat))
    await asyncio.shield(task)
