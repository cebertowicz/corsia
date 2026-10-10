"""Whether a delivered related-past-work pointer was later opened.

``node_start_recall`` remembers what it delivered to each thread; a read tool
that succeeds on one of those pointers logs ``node_start_recall reason=opened``
once. The memory is in-process only: a restart forgets it, so an open after a
restart is not counted.
"""

from __future__ import annotations

from collections import OrderedDict
import threading

from loguru import logger as log

#: Delivered pointers remembered for the opened signal.
DELIVERED_MEMORY = 4096

_delivered: OrderedDict[tuple[str, str], None] = OrderedDict()
_delivered_lock = threading.Lock()


def remember(thread_id: str, pointer: str) -> None:
    """Record that ``thread_id`` was handed ``pointer``."""
    with _delivered_lock:
        _delivered[(thread_id, pointer)] = None
        _delivered.move_to_end((thread_id, pointer))
        while len(_delivered) > DELIVERED_MEMORY:
            _delivered.popitem(last=False)


def note_opened(thread_id: str | None, pointer: str | None) -> None:
    """Log ``opened`` once when a thread reads a pointer it was delivered.

    Call only after the read succeeded: a failed read is not an open.
    """
    if not thread_id or not pointer:
        return
    with _delivered_lock:
        if (thread_id, pointer) not in _delivered:
            return
        del _delivered[(thread_id, pointer)]
    log.info("node_start_recall reason=opened thread_id={}", thread_id)
