"""Per-turn token volume from providers that report a cumulative counter.

Separated from the measurement contract in ``infra.provider_usage``: that
module answers "what did this one payload measure", while this one answers
"what did this turn spend", which is a question about turn lifecycle rather
than about telemetry shape.
"""

from __future__ import annotations

from infra.provider_usage import TokenUsage


class CumulativeTurnVolume:
    """Per-turn volume from a provider that reports a thread-cumulative counter.

    The Codex app-server reports one running total for the whole thread, so a
    turn's own spend is the delta across it. The first update seen for a turn
    already contains that update's own call, which makes ``total - last`` the
    reading the turn started from.

    Readings are held per turn id and consumed once, at that turn's terminal.
    Nothing here reads provider-native fields: callers pass normalized usage,
    so any provider with a cumulative counter can reuse it.

    Not every turn reaches a terminal -- interrupts, process exits, and
    supersession all end turns without one -- so retention is bounded rather
    than trusting every entry to be consumed. That bound is the *only* thing
    releasing those turns: there is deliberately no second release path at the
    out-of-band terminal seam, because a turn ending there writes no ledger
    row, so its entry has nothing to contribute and eviction is the simpler
    owner. The oldest entries go first; a session holds a handful of live
    turns, so the bound sits far above normal use and exists solely to stop a
    long-lived session growing without limit.
    """

    #: Turns retained before the oldest is evicted. Generous by design: this
    #: is a leak guard, not a working-set limit.
    MAX_TRACKED_TURNS = 256

    def __init__(self) -> None:
        self._baseline: dict[str, TokenUsage] = {}
        self._latest: dict[str, TokenUsage] = {}
        self._spent_readings: dict[str, int] = {}

    def _evict_oldest(self) -> None:
        """Drop the oldest tracked turn once the retention bound is passed."""
        while len(self._baseline) > self.MAX_TRACKED_TURNS:
            # dicts preserve insertion order, so the first key is the oldest.
            oldest = next(iter(self._baseline))
            self._baseline.pop(oldest, None)
            self._latest.pop(oldest, None)
            self._spent_readings.pop(oldest, None)

    def observe(
        self,
        turn_id: object,
        *,
        total: TokenUsage | None,
        last: TokenUsage | None,
    ) -> TokenUsage | None:
        """Record a cumulative reading and return the volume it newly spent.

        The first reading's own call is ``last``; every later reading spent
        what the counter advanced by since the previous one. A repeated
        reading spent nothing and returns None, so a replayed notification
        is not a call. Each spent reading is one model call as the provider
        reports them, and ``spent_readings`` counts them for the turn.
        """
        if not isinstance(turn_id, str) or not turn_id or total is None:
            return None
        previous = self._latest.get(turn_id)
        self._latest[turn_id] = total
        if turn_id not in self._baseline:
            self._baseline[turn_id] = total - last if last is not None else total
            self._evict_oldest()
            spent = last
        else:
            spent = total - previous if previous is not None else None
        if spent is None or spent.total == 0:
            return None
        self._spent_readings[turn_id] = self._spent_readings.get(turn_id, 0) + 1
        return spent

    def spent_readings(self, turn_id: object) -> int:
        """How many readings of ``turn_id`` spent volume so far."""
        return self._spent_readings.get(turn_id, 0) if isinstance(turn_id, str) else 0

    def take(self, turn_id: object) -> TokenUsage | None:
        """Consume ``turn_id``'s volume, or None when it was never measured."""
        if not isinstance(turn_id, str) or not turn_id:
            return None
        baseline = self._baseline.pop(turn_id, None)
        latest = self._latest.pop(turn_id, None)
        self._spent_readings.pop(turn_id, None)
        if baseline is None or latest is None:
            return None
        return latest - baseline
