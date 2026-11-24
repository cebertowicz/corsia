"""The recent states a live document's text held, so a comment captured while someone else types still attaches."""
from __future__ import annotations

from collections import deque
from dataclasses import dataclass
from time import monotonic

from doceditor.live_text import shift_spans, state_clocks, utf16_slice

# How many past states a capture may name, and how long after each was replaced.
PAST_STATE_LIMIT = 256
PAST_STATE_SECONDS = 10.0

StateKey = tuple[tuple[tuple[int, int], ...], int, str]
Span = tuple[int, int]


def state_key(vector: bytes, length: int, digest: str) -> StateKey:
    """One document state: its clocks, UTF-16 length and text digest. A delete leaves the clocks unchanged."""
    return tuple(sorted(state_clocks(vector).items())), length, digest


@dataclass
class PastState:
    """One state the live text held, and the edit that replaced it; it holds no text."""
    key: StateKey | None  # set once its transaction ends, when the state can be read
    after: list[tuple[str, int]] | None = None  # ``utf16_delta`` steps to the next state; None while current
    replaced_at: float = 0.0


class PastStates:
    """The live text's recent states, oldest first, newest the current one.

    The document fills it while its Text observer runs: ``replaced`` inside each edit's transaction,
    ``stamp`` after it, since document state cannot be read inside the transaction.
    """

    def __init__(self) -> None:
        self._states: deque[PastState] = deque()
        self.limit, self.seconds = PAST_STATE_LIMIT, PAST_STATE_SECONDS

    def __bool__(self) -> bool:
        return bool(self._states)

    def start(self, key: StateKey) -> None:
        self._states.append(PastState(key))

    def clear(self) -> None:
        self._states.clear()

    def replaced(self, steps: list[tuple[str, int]]) -> None:
        """Inside the transaction: the current state gives way to an unread one."""
        current = self._states[-1]
        current.after, current.replaced_at = steps, monotonic()
        self._states.append(PastState(None))

    def stamp(self, key: StateKey) -> None:
        """After each transaction: record the current state's key, then drop states too old to name."""
        if self._states[-1].key is None:
            self._states[-1].key = key
        elif self._states[-1].key != key:
            # The clocks moved with no text change; the text maps through unchanged.
            self.replaced([])
            self._states[-1].key = key
        cutoff = monotonic() - self.seconds
        while len(self._states) > 1 and (len(self._states) > self.limit or self._states[0].replaced_at < cutoff):
            self._states.popleft()

    def span_now(self, key: StateKey, current: StateKey, text: str, start: int, end: int,
                 quote: str | None) -> Span | None:
        """Where a selection made in the state ``key`` lies in ``text`` (state ``current``), or None when that
        state is unknown or its text changed.

        The live state takes the span as it is. A recent past state maps it forward through every edit since,
        only with the browser's quote, and only when the text there now is still that quote.
        """
        if key == current:
            return (start, end) if quote is None or utf16_slice(text, start, end) == quote else None
        states = list(self._states)
        found = next((index for index in range(len(states) - 1, -1, -1) if states[index].key == key), None)
        if quote is None or found is None or not 0 <= start < end <= key[1]:
            return None
        span = (start, end)
        for state in states[found:-1]:
            span = shift_spans([span], state.after or [])[0]
        return span if utf16_slice(text, *span) == quote else None
