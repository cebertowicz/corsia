"""Runtime-aware reasoning-effort resolution and clamping.

CC valid:    low, medium, high, xhigh, max
Codex valid: none, minimal, low, medium, high, xhigh

Both runtimes default to ``high`` (seeded via delegation_defaults.json).
If a caller supplies an effort value that is invalid for the runtime, the
value is clamped to the nearest valid level and logged; the runtime never
silently receives ``max`` (Codex) or ``none``/``minimal`` (CC).
"""

from __future__ import annotations

import json
import logging
from pathlib import Path

log = logging.getLogger(__name__)

# --- Valid sets ----------------------------------------------------------------

CC_VALID: frozenset[str] = frozenset({"low", "medium", "high", "xhigh", "max"})
CODEX_VALID: frozenset[str] = frozenset({"none", "minimal", "low", "medium", "high", "xhigh"})

_VALID_BY_RUNTIME: dict[str, frozenset[str]] = {
    "cc": CC_VALID,
    "codex": CODEX_VALID,
}

# Explicit per-runtime clamp table: invalid → nearest valid (fail-loud-ish).
# Only values that are truly invalid for the runtime need entries here.
# CC has no "none"/"minimal" — clamp upward to "low".
# Codex has no "max" — clamp downward to "xhigh".
_CLAMP: dict[str, dict[str, str]] = {
    "cc": {"none": "low", "minimal": "low"},
    "codex": {"max": "xhigh"},
}

# --- Seed / settings integration ----------------------------------------------

_SEED_PATH = Path(__file__).parent / "delegation_defaults.json"


def _seed_effort_defaults() -> dict[str, str]:
    """Read per-runtime effort defaults from the JSON seed file."""
    if not _SEED_PATH.exists():
        return {}
    try:
        data = json.loads(_SEED_PATH.read_text())
    except (json.JSONDecodeError, OSError) as e:
        log.warning("effort: failed to read seed %s: %s", _SEED_PATH, e)
        return {}
    return {k: v for k, v in (data.get("effort_defaults") or {}).items()
            if k in _VALID_BY_RUNTIME and v}


def _configured_effort_defaults() -> dict[str, str]:
    """Read per-runtime effort defaults from the delegation settings row."""
    try:
        from infra.settings import get_setting
        configured = (get_setting("delegation") or {}).get("effort_defaults") or {}
        return {k: v for k, v in configured.items() if k in _VALID_BY_RUNTIME and v}
    except Exception as e:
        log.warning("effort: failed to read delegation settings: %s", e)
        return {}


# --- Public API ---------------------------------------------------------------


def clamp_effort(effort: str, runtime: str) -> str:
    """Return ``effort`` clamped to the runtime's valid set.

    If the value is already valid, returns it unchanged. Otherwise maps to
    the nearest valid value via the clamp table and logs a warning.
    """
    valid = _VALID_BY_RUNTIME.get(runtime, frozenset())
    if effort in valid:
        return effort
    clamped = _CLAMP.get(runtime, {}).get(effort)
    if clamped is None:
        # Unknown value not in the clamp table — fall back to "high".
        log.warning(
            "effort: %r is not valid for %s and has no clamp rule; using 'high'",
            effort, runtime,
        )
        return "high"
    log.warning(
        "effort: %r is not valid for %s → clamped to %r",
        effort, runtime, clamped,
    )
    return clamped


def resolve_effort(passed: str | None, runtime: str) -> str:
    """Resolve the reasoning effort for a spawn.

    Resolution order:
    1. Explicitly passed value (caller or session context) — highest priority.
    2. Operator-configured ``delegation.effort_defaults.<runtime>`` setting.
    3. Seed default from ``delegation_defaults.json``.

    The result is always clamped to the runtime's valid set before returning.
    """
    if passed and passed.strip():
        raw = passed.strip()
    else:
        configured = _configured_effort_defaults().get(runtime)
        if configured and configured.strip():
            raw = configured.strip()
        else:
            seed = _seed_effort_defaults().get(runtime)
            raw = (seed or "high").strip()

    return clamp_effort(raw, runtime)
