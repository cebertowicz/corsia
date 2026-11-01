"""Memory-fit window for a local oMLX seat (#1219).

oMLX refuses a prompt whose prefill would not fit in memory before doing any
work. The refusal states the limit L, the memory already in use C and the
KV+SDPA cost K of this prompt of P tokens, so it teaches how many tokens fit:
``P * (L - C_now) / K``. That is a physical runtime fact of one server and one
loaded model, so it lives in process memory only, keyed by (server, model),
and is dropped when the server's observation generation changes or the model
is no longer loaded. ``context_window.resolve_live_window_facts`` caps the
runtime window with it; settings and catalog surfaces keep the configured one.
"""

from __future__ import annotations

import re
from typing import Any, Mapping, NamedTuple

_GIB = 2 ** 30
_MIB = 2 ** 20
# The window moves with live headroom; a settled refusal compares it with the
# window it recorded (``checkpoint_refusal_settlement._recovery_changed``), so
# it only moves in whole steps of this many tokens.
WINDOW_STEP = 1024

_CURRENT_AND_KV = re.compile(
    r"current\s+([\d.]+)\s*GB\s*\+\s*KV\+SDPA\s+([\d.]+)\s*GB", re.IGNORECASE
)
# The scheduler's guard says "the prefill limit is L"; the memory monitor's says
# "<binding> ceiling is L". Both carry the same current and KV+SDPA terms.
_LIMIT = re.compile(r"(?:prefill limit|ceiling) is\s+([\d.]+)\s*GB", re.IGNORECASE)


class _Observation(NamedTuple):
    prompt_tokens: int
    limit: int
    current: int
    kv: int
    used0_mb: int | None
    generation: int


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


def _server_used_mb(server_id: str) -> int | None:
    """The server's oMLX model memory in MiB, as the status sweep last stored it."""
    from llm.network_discovery import get_server

    used = (get_server(server_id) or {}).get("memory_used_mb")
    return int(used) if isinstance(used, int) and used > 0 else None


def record_refusal(
    server_id: str, model_id: str, error_text: str, *, prompt_tokens: int
) -> None:
    """Learn from one refusal for a local managed oMLX model; no-op if unparseable."""
    from llm.server_observation import observation_generation

    key = (server_id, model_id)
    parsed = parse_guard_refusal(error_text)
    if not all(key) or parsed is None or prompt_tokens <= 0 or parsed[2] <= 0:
        return
    limit, current, kv = parsed
    _OBSERVATIONS[key] = _Observation(
        prompt_tokens, limit, current, kv,
        _server_used_mb(key[0]), observation_generation(key[0]),
    )


def _live_observation(server_id: str, model_id: str) -> _Observation | None:
    from llm.network_discovery import get_loaded_model_ids
    from llm.server_observation import observation_generation

    key = (server_id, model_id)
    obs = _OBSERVATIONS.get(key)
    if obs is None:
        return None
    if (observation_generation(server_id) != obs.generation
            or model_id not in get_loaded_model_ids(server_id)):
        # Resolvers run on several threads: drop only the entry this read saw,
        # never one a newer refusal has recorded since.
        if _OBSERVATIONS.get(key) is obs:
            _OBSERVATIONS.pop(key, None)
        return None
    return obs


def memory_fit_window(row: Mapping[str, Any]) -> int | None:
    """Tokens that fit in memory now for this catalog row, else None."""
    key = split_omlx_selector(row.get("litellm_model"))
    obs = _live_observation(*key) if key else None
    if key is None or obs is None:
        return None
    used_now = _server_used_mb(key[0])
    delta = (used_now - obs.used0_mb) * _MIB if used_now and obs.used0_mb else 0
    headroom = obs.limit - (obs.current + delta)
    fit = int(obs.prompt_tokens * headroom / obs.kv) if headroom > 0 else 0
    # Never zero: callers treat a zero window as "no window known".
    return max(WINDOW_STEP, fit // WINDOW_STEP * WINDOW_STEP)
