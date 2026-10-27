"""A frozen wall clock, so a fixture's relative windows never drift with the date.

Routes compute "last 7 days" style windows from ``datetime.now``. The fixture's
timestamps are absolute, so the harness freezes ``datetime`` and ``time.time``
before the application is imported; otherwise the cost of a windowed read would
shrink as the fixture aged and a regression would hide behind the calendar.
"""

from __future__ import annotations

import datetime as _dt
import time as _time
from types import ModuleType, SimpleNamespace

#: The instant every harness run believes it is (UTC). Fixture rows end here.
FROZEN_NOW = _dt.datetime(2026, 10, 8, 12, 0, 0, tzinfo=_dt.timezone.utc)


def install() -> None:
    """Replace ``datetime.datetime`` and ``time.time`` with frozen versions."""
    real = _dt.datetime

    def _as(cls, moment):
        return cls(moment.year, moment.month, moment.day, moment.hour, moment.minute,
                   moment.second, moment.microsecond, moment.tzinfo)

    class FrozenDatetime(real):  # type: ignore[misc, valid-type]
        @classmethod
        def now(cls, tz=None):
            if tz is None:
                return _as(cls, FROZEN_NOW.replace(tzinfo=None))
            return _as(cls, FROZEN_NOW.astimezone(tz))

        @classmethod
        def utcnow(cls):
            return _as(cls, FROZEN_NOW.replace(tzinfo=None))

    _dt.datetime = FrozenDatetime  # type: ignore[misc]
    _time.time = lambda: FROZEN_NOW.timestamp()


def freeze_reuse_window(join: ModuleType) -> None:
    """Stop the Measurement join's reuse window from expiring, so route order alone decides a reuse.

    ``/where-to-look`` reuses a payload read that finished less than ``RETENTION_SECONDS`` ago on the
    monotonic clock, usually the one an earlier ``/api/measurement/*`` route left. On a loaded host the
    gap between that route and this one's measured call can cross the window, and the same tree then
    measures the reuse on one run and the full recompute on the next. ``install`` cannot freeze
    ``time.monotonic`` itself, because the event loop schedules by it, so only the join's clock is held.
    """
    if getattr(join, "time", None) is not _time:
        raise RuntimeError(f"{join.__name__} no longer reads the clock as time.monotonic(), so its reuse "
                           "window would expire by wall time again; freeze the clock it reads now")
    join.time = SimpleNamespace(monotonic=lambda: 0.0)  # type: ignore[attr-defined]
