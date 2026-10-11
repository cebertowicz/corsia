"""One page load's repeated Measurement reads, run once.

A page load asks ``/series`` and ``/agents`` for its panels and ``/where-to-look`` ranks
the same two payloads, for the same viewer, scope and calendar. A payload decorated with
``joined_read`` runs here under a key built from every argument it takes, so no input can
be left out of it; a ``user`` is keyed by its id, the only field the payloads read. An
identical read still in flight is joined instead of run again; ``/where-to-look`` alone
also takes one that finished less than ``RETENTION_SECONDS`` ago, so its lists rank the
figures the page just drew. A page read never takes a finished result: a reload reads the
database again.

The first caller runs the read itself, in its own request, so the read reaches the
database lane exactly when it did before the join. Later callers await its result
shielded, so one of them leaving never touches the read. If the first caller leaves
before the read finishes, its read is cancelled with it and the callers still waiting
run it again, one of them first; when every caller has left, nothing is left running and
the key is gone. A read that fails is never kept: every caller waiting on it gets the
same error and the next read runs again. Finished results are bounded by count and
approximate size, oldest first; a result that cannot be sized still reaches every caller
but is not kept. Callers share the result and must not mutate it.
"""

from __future__ import annotations

import asyncio
import functools
import inspect
import json
import logging
import time
import weakref
from collections.abc import Awaitable, Callable, Hashable
from dataclasses import dataclass
from typing import Any, ParamSpec, TypeVar

P = ParamSpec("P")
T = TypeVar("T")

logger = logging.getLogger(__name__)

RETENTION_SECONDS = 30.0
MAX_ENTRIES = 32
MAX_BYTES = 8 * 1024 * 1024


class _Abandoned(Exception):
    """The first caller left before its read finished: a waiting caller runs it again."""


@dataclass(eq=False)
class _Flight:
    result: asyncio.Future[Any]
    finished_at: float | None = None
    size: int = 0


_flights: weakref.WeakKeyDictionary[asyncio.AbstractEventLoop, dict[Hashable, _Flight]] = weakref.WeakKeyDictionary()


def key_of(read: Callable[..., Any], *args: Any, **kwargs: Any) -> tuple[Hashable, ...]:
    """The key of one call: the read and every argument it binds, a list as a tuple and a ``user`` as its id."""
    bound = inspect.signature(read).bind(*args, **kwargs)
    bound.apply_defaults()
    keyed = {name: value["id"] if name == "user" else tuple(value) if isinstance(value, list) else value
             for name, value in bound.arguments.items()}
    return (read.__module__, read.__qualname__, *keyed.items())


def joined_read(read: Callable[P, Awaitable[T]]) -> Callable[..., Awaitable[T]]:
    """``read`` joined with identical calls; a caller may pass ``reuse_finished=True`` (see ``joined``)."""

    @functools.wraps(read)
    async def wrapper(*args: Any, reuse_finished: bool = False, **kwargs: Any) -> T:
        return await joined(key_of(read, *args, **kwargs), lambda: read(*args, **kwargs), reuse_finished=reuse_finished)

    return wrapper


async def joined(key: Hashable, compute: Callable[[], Awaitable[T]], *, reuse_finished: bool = False) -> T:
    """``compute()``'s result, shared with every identical read in flight (and, with ``reuse_finished``, one just done)."""
    loop = asyncio.get_running_loop()
    flights = _flights.setdefault(loop, {})
    while True:
        _evict(flights, time.monotonic())
        flight = flights.get(key)
        if flight is None or (flight.finished_at is not None and not reuse_finished):
            return await _lead(flights, key, compute, loop)
        try:
            return await asyncio.shield(flight.result)
        except _Abandoned:
            continue


def _result_size(result: Any) -> int:
    """Approximate a joined result's retained payload size without running serialization on the event loop."""
    return len(json.dumps(result, default=str))


async def _lead(flights: dict[Hashable, _Flight], key: Hashable, compute: Callable[[], Awaitable[T]],
                loop: asyncio.AbstractEventLoop) -> T:
    flight = _Flight(loop.create_future())
    flights[key] = flight
    try:
        result = await compute()
    except BaseException as error:
        _drop(flights, key, flight)
        flight.result.set_exception(_Abandoned() if isinstance(error, asyncio.CancelledError) else error)
        flight.result.exception()  # every waiter has it; never logged as unretrieved
        raise
    flight.finished_at = time.monotonic()
    flight.result.set_result(result)
    try:
        flight.size = await asyncio.to_thread(_result_size, result)
    except Exception as error:  # e.g. a non-str dict key: the callers have the result, only keeping it is skipped
        logger.warning("Measurement read result not kept for reuse: cannot size it (%s)", type(error).__name__)
        _drop(flights, key, flight)
        return result
    _evict(flights, flight.finished_at)
    return result


def _drop(flights: dict[Hashable, _Flight], key: Hashable, flight: _Flight) -> None:
    if flights.get(key) is flight:
        del flights[key]


def _evict(flights: dict[Hashable, _Flight], now: float) -> None:
    """Drop expired results, then the oldest finished ones until count and size fit."""
    finished = sorted(((flight.finished_at, key) for key, flight in flights.items() if flight.finished_at is not None),
                      key=lambda item: item[0])
    for finished_at, key in finished:
        if now - finished_at >= RETENTION_SECONDS:
            del flights[key]
    for _, key in finished:
        if key not in flights:
            continue
        held = sum(flight.size for flight in flights.values())
        if len(flights) <= MAX_ENTRIES and held <= MAX_BYTES:
            break
        del flights[key]
