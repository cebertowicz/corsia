"""Memory-fit window for a local oMLX seat (#1219).

oMLX refuses a prompt whose prefill would not fit in memory before doing any
work. The refusal states the limit L, the memory already in use C and the
KV+SDPA cost K of this prompt of P tokens, so it teaches how many tokens fit:
``P * (L - C) / K``. A refusal between prefill chunks instead names the N tokens
already held when the next chunk would not fit, so N fit. C includes whatever
else the server is doing at that moment, so the fact holds only while the
refused Turn recovers: it lives in
process memory, keyed by (server, model), and ``end_turn`` drops it when that
Turn ends. ``context_window.resolve_live_window_facts`` caps the runtime window
with it; settings and catalog surfaces keep the configured one.
"""

from __future__ import annotations

import re
from typing import Any, Mapping, NamedTuple

_UNITS = {"GB": 2 ** 30, "MB": 2 ** 20, "KB": 2 ** 10}
_AMOUNT = r"([\d.]+)\s*(GB|MB|KB)"

# oMLX 0.7.1rc1 refuses in four wordings, all "prefill memory guard rejected":
# the scheduler's "(current C + KV+SDPA K) but the prefill limit is L"; the memory
# monitor's "... but <binding> ceiling is L"; the preflight safety guard's
# "(current C + KV K + min-chunk transient T) but prefill safety cap is L"; and
# the chunked-step guard's "pre-chunk guard at P tokens, kv_len=N ... prefill safety
# cap L", which names no current or KV term but N, the tokens already held.
_CURRENT = re.compile(r"current\s+" + _AMOUNT, re.IGNORECASE)
_KV = re.compile(r"\+\s*KV(?:\+SDPA)?\s+" + _AMOUNT, re.IGNORECASE)
_TRANSIENT = re.compile(r"\+\s*min-chunk transient\s+" + _AMOUNT, re.IGNORECASE)
_LIMIT = re.compile(r"(?:prefill limit|ceiling|prefill safety cap) is\s+" + _AMOUNT, re.IGNORECASE)
_HELD = re.compile(r"pre-chunk guard at \d+ tokens, kv_len=(\d+)", re.IGNORECASE)


def _bytes(match: re.Match[str]) -> int:
    return int(float(match.group(1)) * _UNITS[match.group(2).upper()])


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
    """(limit, current, kv) in bytes from a prefill-guard refusal, else None.

    A min-chunk transient does not grow with the prompt, so it counts as memory
    already in use rather than as KV.
    """
    current, kv, limit = _CURRENT.search(text), _KV.search(text), _LIMIT.search(text)
    if not current or not kv or not limit:
        return None
    transient = _TRANSIENT.search(text)
    try:
        return (
            _bytes(limit),
            _bytes(current) + (_bytes(transient) if transient else 0),
            _bytes(kv),
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
    turn_id = _current_turn_id()
    fit = _fit_tokens(error_text, prompt_tokens)
    if not all(key) or turn_id is None or fit is None:
        return
    # Never zero: callers treat a zero window as "no window known".
    _OBSERVATIONS[key] = _Observation(turn_id, max(1, fit))


def _fit_tokens(error_text: str, prompt_tokens: int) -> int | None:
    parsed = parse_guard_refusal(error_text)
    if parsed is None:
        held = _HELD.search(error_text)
        return int(held.group(1)) if held else None
    limit, current, kv = parsed
    if prompt_tokens <= 0 or kv <= 0:
        return None
    return int(prompt_tokens * (limit - current) / kv) if limit > current else 0


def end_turn(turn_id: str) -> None:
    """Drop what ``turn_id`` learned, leaving a newer Turn's observation in place.

    Runs once the Turn's terminal write exits: its commit when ``finish_turn``
    opened the transaction, before the outer commit when a caller passed one
    (an outer rollback then leaves the Turn running without its cap until it
    is refused again). A retried terminal finds nothing left to drop.
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
