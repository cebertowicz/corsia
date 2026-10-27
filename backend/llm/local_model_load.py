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


def _fit(db, server_id: str, model_id: str, loaded: set[str]) -> Readiness:
    """Read the seat and reported sizes together, then use the shared fit verdict."""
    from llm.locality import server_on_this_mac

    others = sorted(loaded - {model_id})
    selectors = {f"omlx@{server_id}/{name}": name for name in [model_id, *others]}
    marks = ",".join("?" * len(selectors))
    rows = db.execute(
        f"SELECT s.id, s.status, s.kind, s.port, s.metadata_json AS server_metadata, "
        f"e.host, m.litellm_model, m.metadata_json AS model_metadata "
        f"FROM network_servers s LEFT JOIN network_server_endpoints e ON e.server_id=s.id "
        f"LEFT JOIN models m ON m.litellm_model IN ({marks}) WHERE s.id=?",
        [*selectors, server_id],
    ).fetchall()
    if not rows or rows[0]["status"] != "online":
        return Readiness("unreachable")
    server = {"id": server_id, "kind": rows[0]["kind"], "port": rows[0]["port"],
              "metadata_json": rows[0]["server_metadata"]}
    endpoints = [{"host": host} for host in dict.fromkeys(row["host"] for row in rows if row["host"])]
    sizes: dict[str, int | None] = {name: None for name in selectors.values()}
    for row in rows:
        selector = row["litellm_model"]
        if selector is None:
            continue
        try:
            size = int((json.loads(row["model_metadata"] or "{}")).get("size_bytes") or 0)
        except (TypeError, ValueError, AttributeError):
            log.warning("Unreadable catalog metadata for %s: treating its size as unreported", selector)
            size = 0
        sizes[selectors[selector]] = size if size > 0 else None
    target_size = sizes[model_id] if server_on_this_mac(server, endpoints) else None
    verdict = load_verdict(target_size, [sizes[name] for name in others], model_room_bytes())
    return Readiness(verdict, len(others))


def omlx_seat_and_model(target: ExecutionTarget) -> tuple[str, str] | None:
    """Identify the oMLX seat and model for a native network target."""
    from llm.locality import row_server_id

    if not (target.harness == "native" and target.provider == "omlx" and target.backend == "network"
            and target.transport_model.startswith("omlx@")):
        return None
    server_id = row_server_id({"litellm_model": target.transport_model})
    model_id = target.transport_model.partition("/")[2]
    return (server_id, model_id) if server_id and model_id else None


def cached_status(target: ExecutionTarget, *, db, server_id: str) -> str:
    """Return the network catalog's cached status for this seat."""
    from llm.model_status import _network_status
    from llm.network_discovery import get_loaded_model_ids

    server = db.execute("SELECT status FROM network_servers WHERE id=?", (server_id,)).fetchone()
    return _network_status({"litellm_model": target.transport_model, "available": True},
                           {server_id: server["status"]} if server else {}, get_loaded_model_ids)


def readiness(target: ExecutionTarget, *, db) -> Readiness | None:
    """Project send admission's cached readiness and the shared fit verdict."""
    from llm.network_discovery import get_loaded_model_ids

    seat = omlx_seat_and_model(target)
    if seat is None or (not target.available and target.availability_reason not in {
        "model_not_loaded", "model_unavailable", "fits_now", "no_room", "no_memory", "unreachable",
    }):
        return None
    server_id, model_id = seat
    status = cached_status(target, db=db, server_id=server_id)
    loaded = get_loaded_model_ids(server_id)
    if status == "ready":
        return Readiness("loaded", len(loaded - {model_id}))
    fit = _fit(db, server_id, model_id, loaded)
    return fit if fit.state == "unreachable" or status == "available" else None


def _readiness_now(target: ExecutionTarget) -> Readiness | None:
    from infra.database import get_db

    db = get_db()
    try:
        return readiness(target, db=db)
    finally:
        db.close()


def _fit_now(server_id: str, model_id: str, loaded: set[str]) -> Readiness:
    from infra.database import get_db

    db = get_db()
    try:
        return _fit(db, server_id, model_id, loaded)
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


def _emit_load(server_id: str, model_id: str, action: str, error: str | None = None) -> None:
    from infra.product_events import emit_product_event

    properties = {"provider": "omlx", "model_id": model_id, "server_id": server_id, "automatic": True}
    emit_product_event("model", action, properties={**properties, **({"error": error} if error else {})})


async def _server_is_up(server_id: str) -> bool:
    from llm.providers.omlx_lifecycle import check_server

    return bool((await check_server({"server_id": server_id})).get("running"))


async def _load(target: ExecutionTarget, server_id: str, model_id: str) -> None:
    """One load, under the seat's lock, rechecking the fit the lock was waited for."""
    from infra.db_async import run_in_db
    from llm.network_discovery import notify_cached_model_loaded
    from llm.providers.omlx_lifecycle import load_model, read_loaded
    from llm.server_observation import observation_generation

    async with seat_lock(server_id):
        generation = observation_generation(server_id)
        live = await read_loaded({"server_id": server_id})
        if live is None:
            _emit_load(server_id, model_id, "load.failed", "unreachable")
            raise ModelLoadFailed("unreachable")
        loaded = {row["identifier"] for row in live}
        if model_id in loaded:
            return
        state = (await run_in_db(_fit_now, server_id, model_id, loaded)).state
        if state != "fits_now":
            log.info("Not loading %s on %s: %s", model_id, server_id, state)
            _emit_load(server_id, model_id, "load.failed", state)
            raise ModelLoadFailed(state)
        result = await load_model({"server_id": server_id}, model_id)
        if not result.get("ok"):
            up = await _server_is_up(server_id)
            log.warning("oMLX couldn't load %s on %s (%s): %s", model_id, server_id,
                        "server up" if up else "server unreachable", result.get("message"))
            cause: LoadFailure = "load_failed" if up else "unreachable"
            _emit_load(server_id, model_id, "load.failed", cause)
            raise ModelLoadFailed(cause)
        notify_cached_model_loaded(server_id, model_id, generation)
        _emit_load(server_id, model_id, "load.completed")


async def _ensure(target: ExecutionTarget, server_id: str, model_id: str) -> None:
    from infra.db_async import run_in_db
    from llm.providers.omlx_lifecycle import read_loaded

    state = await run_in_db(_readiness_now, target)
    if state is None:
        return
    live = await read_loaded({"server_id": server_id})
    if live is None:
        log.info("Cannot read loaded status for %s on %s", model_id, server_id)
        _emit_load(server_id, model_id, "load.failed", "unreachable")
        raise ModelLoadFailed("unreachable")
    if any(row["identifier"] == model_id for row in live):
        return
    await _load(target, server_id, model_id)


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
    seat = omlx_seat_and_model(target)
    if seat is None:
        return
    task = _LOADS.get(seat)
    if task is None:
        task = asyncio.create_task(_ensure(target, *seat))
        _LOADS[seat] = task
        task.add_done_callback(partial(_settled, seat))
    await asyncio.shield(task)
