"""Keep threads being deleted from taking input or starting a Turn (#2647).

Deleting a family stops its members one by one. Without a fence, each stop's
news to a still-live parent, or a person's message, could start a Turn after
the user asked for the delete. ``fence`` marks the members for the length of
the delete, and ``infra.thread_inputs.thread_admission_state`` -- the read every
input writer and ``start_idle_turn`` make inside their write -- reports a
fenced thread unavailable: a sender that asked for loud refusals gets
``BEING_DELETED``, a writer that tolerates an unavailable thread skips it.

The marks are process-local. One uvicorn process admits every input; the MCP
server and CLIs reach it over HTTP.
"""

from __future__ import annotations

import threading
from collections import Counter
from collections.abc import Iterable, Iterator
from contextlib import contextmanager

#: What a sender whose input a fence refused is told.
BEING_DELETED = "thread is being deleted"

_lock = threading.Lock()
_fenced: Counter[str] = Counter()


def is_fenced(thread_id: str) -> bool:
    """Whether a delete in progress holds this thread."""
    return _fenced[thread_id] > 0


class Fence:
    """The threads one delete holds; each is held once however often it is added."""

    def __init__(self) -> None:
        self.held: set[str] = set()

    def add(self, thread_ids: Iterable[str]) -> None:
        """Hold ``thread_ids``; call it inside a write so no admission straddles it."""
        with _lock:
            for thread_id in set(thread_ids) - self.held:
                self.held.add(thread_id)
                _fenced[thread_id] += 1


@contextmanager
def fence() -> Iterator[Fence]:
    """Hold threads for one delete and release every one however the delete ends."""
    held = Fence()
    try:
        yield held
    finally:
        with _lock:
            for thread_id in held.held:
                _fenced[thread_id] -= 1
                if _fenced[thread_id] <= 0:
                    del _fenced[thread_id]
