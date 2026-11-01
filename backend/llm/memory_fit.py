"""Memory-fit window for a local oMLX seat (#1219).

oMLX refuses a prompt whose prefill would not fit in memory before doing any
work. The refusal states the limit L, the memory already in use C and the
KV+SDPA cost K of this prompt of P tokens, so it teaches how many tokens fit:
``P * (L - C) / K``. C includes whatever else the server is doing at that
moment, so the fact holds only while the refused Turn recovers: it lives in
process memory, keyed by (server, model), and ``end_turn`` drops it when that
Turn ends. ``context_window.resolve_live_window_facts`` caps the runtime window
with it; settings and catalog surfaces keep the configured one.
"""

from __future__ import annotations

import re
from typing import Any, Mapping, NamedTuple

_GIB = 2 ** 30

_CURRENT_AND_KV = re.compile(
    r"current\s+([\d.]+)\s*GB\s*\+\s*KV\+SDPA\s+([\d.]+)\s*GB", re.IGNORECASE
)
# The scheduler's guard says "the prefill limit is L"; the memory monitor's says
# "<binding> ceiling is L". Both carry the same current and KV+SDPA terms.
_LIMIT = re.compile(r"(?:prefill limit|ceiling) is\s+([\d.]+)\s*GB", re.IGNORECASE)


class _Observation(NamedTuple):
    turn_id: str
    fit: int


_OBSERVATIONS: dict[tuple[str, str], _Observation] = {}


def reset_for_tests() -> None:
    _OBSERVATIONS.clear()


def split_omlx_selector(selector: Any) -> tuple[str, str] | None:
    """``omlx@<server_id>/<model_id>`` -> (server_id, model_id), else None."""
    text = str(selector or "")
    if not text.startswith("omlx@"):
        return None
    server_id, _, model_id = text[len("omlx@"):].partition("/")
    return (server_id, model_id) if server_id and model_id else None


def parse_guard_refusal(text: str) -> tuple[int, int, int] | None:
    """(limit, current, kv) in bytes from a prefill-guard refusal, else None."""
    current_kv = _CURRENT_AND_KV.search(text)
    limit = _LIMIT.search(text)
    if not current_kv or not limit:
        return None
    try:
        return (
            int(float(limit.group(1)) * _GIB),
            int(float(current_kv.group(1)) * _GIB),
            int(float(current_kv.group(2)) * _GIB),
        )
    except ValueError:
        return None


def _current_turn_id() -> str | None:
    from infra.state.context import _current_turn_identity

    turn_id = getattr(_current_turn_identity.get(), "turn_id", None)
    return turn_id if isinstance(turn_id, str) and turn_id else None


def record_refusal(
    server_id: str, model_id: str, error_text: str, *, prompt_tokens: int
) -> None:
    """Learn from one refusal for a local managed oMLX model; no-op if unparseable.

    Outside a Turn nothing would end the observation, so nothing is recorded.
    """
    key = (server_id, model_id)
    parsed = parse_guard_refusal(error_text)
    turn_id = _current_turn_id()
    if not all(key) or turn_id is None or parsed is None or prompt_tokens <= 0 or parsed[2] <= 0:
        return
    limit, current, kv = parsed
    fit = int(prompt_tokens * (limit - current) / kv) if limit > current else 0
    # Never zero: callers treat a zero window as "no window known".
    _OBSERVATIONS[key] = _Observation(turn_id, max(1, fit))


def end_turn(turn_id: str) -> None:
    """Drop what ``turn_id`` learned, leaving a newer Turn's observation in place.

    Runs once the Turn's terminal write commits; a retried terminal finds
    nothing left to drop.
    """
    for key, obs in list(_OBSERVATIONS.items()):
        # Resolvers run on several threads: drop only the entry this read saw.
        if obs.turn_id == turn_id and _OBSERVATIONS.get(key) is obs:
            _OBSERVATIONS.pop(key, None)


def memory_fit_window(row: Mapping[str, Any]) -> int | None:
    """Tokens that fit in memory for this catalog row while a refused Turn runs."""
    key = split_omlx_selector(row.get("litellm_model"))
    obs = _OBSERVATIONS.get(key) if key else None
    return obs.fit if obs else None
