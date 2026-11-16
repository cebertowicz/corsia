"""Aggregate timings and counts for one connected browser pane: sizes, counters and delays, never content.

Each connected pane logs one INFO line a minute, and one when it leaves, so a remote session can be read
afterwards with the conductor's ``log_read``. The line carries no URL, page text, pointer position or key
value. Delays use the HTTP timing histogram's buckets and its percentile estimate, so a figure here reads
like one there. One process-wide 100 ms ticker measures event-loop lateness and times the flushes; it runs
only while at least one pane is connected.
"""
from __future__ import annotations

import asyncio
import time
from bisect import bisect_left
from typing import TYPE_CHECKING

from loguru import logger

from infra.local_connection import is_direct_loopback
from infra.request_timing import _BUCKET_UPPERS, bucket_percentile

if TYPE_CHECKING:
    from host_access.browser_pane import PaneAppearance

WINDOW_SECONDS = 60.0
TICK_SECONDS = 0.1
#: A frame this long after Chrome handled an input is no longer that input's effect, so it carries no stamp.
STAMP_MAX_AGE_SECONDS = 2.0
_MAX_COUNT = 100_000
_MAX_SEQUENCE = 2**31
_MAX_IN_FLIGHT_SAMPLES = 64
#: Input that can change the picture; a pointer move rarely does, so it is timed but never stamped.
_STAMPED_KINDS = frozenset({"click", "wheel", "key"})
_BUCKETS = len(_BUCKET_UPPERS)


class Histogram:
    """Counts per HTTP-timing bucket; frame sizes are recorded in KiB so they fit the same buckets."""

    def __init__(self) -> None:
        self.buckets = [0] * _BUCKETS

    def add(self, value: float) -> None:
        self.buckets[min(bisect_left(_BUCKET_UPPERS, value), _BUCKETS - 1)] += 1

    def merge(self, counts: list[int]) -> None:
        for index, count in enumerate(counts):
            self.buckets[index] += count

    @property
    def count(self) -> int:
        return sum(self.buckets)

    def percentile(self, fraction: float) -> float:
        return bucket_percentile(self.buckets, fraction)


def _count(value: object, ceiling: int = _MAX_COUNT) -> bool:
    return isinstance(value, int) and not isinstance(value, bool) and 0 <= value <= ceiling


def _bucket_counts(value: object) -> list[int] | None:
    if isinstance(value, list) and len(value) == _BUCKETS and all(_count(item) for item in value):
        return value
    return None


def parse_report(data: dict) -> dict | None:
    """The pane's own figures for its last interval, or None unless every field is in range."""
    painted, painted_seq = data.get("painted"), data.get("painted_seq")
    buffered, visible = data.get("up_buffered_max"), data.get("visible")
    decode, delay = _bucket_counts(data.get("decode_ms")), _bucket_counts(data.get("input_to_paint_ms"))
    if not (_count(painted) and _count(painted_seq, _MAX_SEQUENCE) and _count(buffered, _MAX_SEQUENCE)
            and isinstance(visible, bool) and decode is not None and delay is not None):
        return None
    return {"painted": painted, "painted_seq": painted_seq, "buffered": buffered, "visible": visible,
            "decode": decode, "delay": delay}


def input_kind(data: dict) -> str | None:
    """The kind of one pane input message, or None for anything else."""
    if data.get("type") == "input_keyboard":
        return "key"
    if data.get("type") != "input_mouse":
        return None
    event_type = data.get("eventType")
    if event_type == "mouseMoved":
        return "move"
    return "wheel" if event_type == "mouseWheel" else "click"


class PaneStats:
    """One pane connection's figures for the current window."""

    def __init__(self, thread_id: str, viewer: str) -> None:
        self.thread = thread_id[:8]
        self.viewer = viewer
        self.sent_seq = 0
        self._size = "unknown"
        self._handled: tuple[int, float] | None = None
        self._painted_seq = 0
        self._reset_window()

    def _reset_window(self) -> None:
        self._started = time.monotonic()
        self._offered = self._replaced = self._sent = self._sent_bytes = 0
        self._frame_kib, self._send_ms, self._cpu_ms, self._lag_ms = (Histogram() for _ in range(4))
        self._dispatch = {kind: Histogram() for kind in ("move", "click", "key", "wheel")}
        self._inputs = {"move": 0, "click": 0, "key": 0, "wheel": 0}
        self._coalesced = self._backlog_max = 0
        self._reports = self._bad_reports = self._painted = self._buffered_max = 0
        self._hidden = False
        self._decode_ms, self._delay_ms = Histogram(), Histogram()
        self._in_flight: list[int] = []

    def appearance(self, appearance: PaneAppearance | None) -> None:
        if appearance is not None:
            self._size = f"{appearance.width}x{appearance.height}@{appearance.scale:g}"

    def frame_offered(self, size: int, cpu_ms: float, replaced: bool) -> None:
        self._offered += 1
        self._replaced += replaced
        self._frame_kib.add(size / 1024)
        self._cpu_ms.add(cpu_ms)

    def stamp(self, message: str) -> str:
        """Number a frame about to be sent, and name the input Chrome just handled, by appending two keys."""
        self.sent_seq += 1
        fields = f',"seq":{self.sent_seq}'
        if self._handled is not None:
            input_id, handled_at = self._handled
            self._handled = None
            if time.monotonic() - handled_at <= STAMP_MAX_AGE_SECONDS:
                fields += f',"input":{input_id}'
        return message[:-1] + fields + "}" if message.endswith("}") else message

    def frame_sent(self, send_ms: float, size: int) -> None:
        self._sent += 1
        self._sent_bytes += size
        self._send_ms.add(send_ms)

    def input_handled(self, data: dict, dispatch_seconds: float) -> None:
        kind = input_kind(data)
        if kind is None:
            return
        self._inputs[kind] += 1 if kind != "click" or data.get("eventType") == "mousePressed" else 0
        self._dispatch[kind].add(dispatch_seconds * 1000)
        input_id = data.get("id")
        if kind in _STAMPED_KINDS and _count(input_id, _MAX_SEQUENCE) and input_id:
            self._handled = (input_id, time.monotonic())

    def input_coalesced(self) -> None:
        self._coalesced += 1

    def backlog(self, queued: int) -> None:
        self._backlog_max = max(self._backlog_max, queued)

    def loop_lag(self, milliseconds: float) -> None:
        self._lag_ms.add(milliseconds)

    def client_report(self, data: dict) -> None:
        report = parse_report(data)
        if report is None:
            self._bad_reports += 1
            return
        self._reports += 1
        self._painted += report["painted"]
        self._painted_seq = max(self._painted_seq, report["painted_seq"])
        self._buffered_max = max(self._buffered_max, report["buffered"])
        self._hidden = self._hidden or not report["visible"]
        self._decode_ms.merge(report["decode"])
        self._delay_ms.merge(report["delay"])
        if len(self._in_flight) < _MAX_IN_FLIGHT_SAMPLES:
            self._in_flight.append(max(0, self.sent_seq - self._painted_seq))

    def due(self, now: float) -> bool:
        return now - self._started >= WINDOW_SECONDS

    def flush(self, final: bool = False) -> None:
        """Log the window and start the next; a window with no frames, input or report logs nothing."""
        seconds = max(time.monotonic() - self._started, 0.001)
        active = self._offered or self._reports or self._bad_reports or any(self._inputs.values())
        line = self._line(seconds, final) if active else None
        self._reset_window()
        if line:
            logger.info("browser pane stats {}", line)

    def _line(self, seconds: float, final: bool) -> str:
        def p(histogram: Histogram, fraction: float) -> str:
            return f"{histogram.percentile(fraction):.1f}"

        tokens = [
            f"thread={self.thread}", f"window={seconds:.0f}s", f"viewer={self.viewer}", f"size={self._size}",
            f"frames_in={self._offered}", f"replaced={self._replaced}", f"sent={self._sent}",
            f"sent_fps={self._sent / seconds:.1f}",
            f"sent_kib={self._sent_bytes / 1024:.0f}",
            f"frame_kib_p50={p(self._frame_kib, .5)}", f"frame_kib_p95={p(self._frame_kib, .95)}",
            f"send_ms_p50={p(self._send_ms, .5)}", f"send_ms_p95={p(self._send_ms, .95)}",
            f"frame_cpu_ms_p50={p(self._cpu_ms, .5)}", f"frame_cpu_ms_p95={p(self._cpu_ms, .95)}",
            f"loop_lag_ms_p95={p(self._lag_ms, .95)}",
            f"moves={self._inputs['move']}", f"coalesced={self._coalesced}", f"clicks={self._inputs['click']}",
            f"keys={self._inputs['key']}", f"wheels={self._inputs['wheel']}",
            f"dispatch_ms_move_p95={p(self._dispatch['move'], .95)}",
            f"dispatch_ms_click_p95={p(self._dispatch['click'], .95)}",
            f"dispatch_ms_key_p95={p(self._dispatch['key'], .95)}",
            f"dispatch_ms_wheel_p95={p(self._dispatch['wheel'], .95)}", f"backlog_max={self._backlog_max}",
            f"reports={self._reports}",
        ]
        if self._reports:
            flights = sorted(self._in_flight)
            tokens += [
                f"painted={self._painted}", f"painted_fps={self._painted / seconds:.1f}",
                f"in_flight_p50={flights[len(flights) // 2]}", f"in_flight_max={flights[-1]}",
                f"decode_ms_p95={p(self._decode_ms, .95)}", f"up_buffered_max={self._buffered_max}",
                f"input_to_paint_ms_p50={p(self._delay_ms, .5)}", f"input_to_paint_ms_p95={p(self._delay_ms, .95)}",
                f"input_to_paint_n={self._delay_ms.count}", f"visible={'false' if self._hidden else 'true'}",
            ]
        if self._bad_reports:
            tokens.append(f"bad_reports={self._bad_reports}")
        if final:
            tokens.append("final=1")
        return " ".join(tokens)


_active: set[PaneStats] = set()
_ticker: asyncio.Task | None = None


def viewer_kind(pane: object) -> str:
    """Whether the pane connects directly from this computer, not through a forwarder on it; no address is kept."""
    if getattr(getattr(pane, "client", None), "host", None) is None:
        return "unknown"
    return "local" if is_direct_loopback(pane) else "remote"  # type: ignore[arg-type]


def start(stats: PaneStats) -> None:
    """Register a connected pane, starting the shared ticker if it is not running on this loop."""
    global _ticker
    _active.add(stats)
    if _ticker is None or _ticker.done() or _ticker.get_loop() is not asyncio.get_running_loop():
        _ticker = asyncio.create_task(_tick(), name="browser-pane-stats")


def stop(stats: PaneStats) -> None:
    """Log the pane's last window and drop it; the ticker ends with the last pane."""
    global _ticker
    if stats not in _active:
        return
    _active.discard(stats)
    stats.flush(final=True)
    if not _active and _ticker is not None:
        _ticker.cancel()
        _ticker = None


async def _tick() -> None:
    loop = asyncio.get_running_loop()
    while True:
        expected = loop.time() + TICK_SECONDS
        await asyncio.sleep(TICK_SECONDS)
        now = loop.time()
        lag = max(0.0, (now - expected) * 1000)
        for stats in tuple(_active):
            stats.loop_lag(lag)
            if stats.due(time.monotonic()):
                stats.flush()
