
from __future__ import annotations

from .event_log import event_log
from .subprocess_log import subprocess_log


INSTRUMENTS: dict[str, callable] = {
    "event_log": event_log,
    "subprocess_log": subprocess_log,
}


__all__ = ["INSTRUMENTS", "event_log", "subprocess_log"]
