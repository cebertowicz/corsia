"""oMLX's server-wide settings against CORSIA's recommended ones, read once per discovery.

The read rides oMLX's existing catalog discovery and is kept until the next
discovery of that server, so a model-sources read never calls oMLX. A failed or
empty read stores nothing: the difference is then unknown, never an error.

While the person lets CORSIA manage their oMLX (``own_omlx_management``), the
read also covers the one model CORSIA downloaded there, and the differences
widen to ``prefill_priority`` and that model's settings. Nothing else is ever
offered: not their key, sign-in, network, port or folders, ``is_default``, or
any other model.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
from typing import Any

from llm.local_model import LocalModel
from llm.local_profiles import MODEL_SETTINGS, OMLX_GLOBAL_SETTINGS
from llm.providers.omlx_settings import get_model_settings, get_settings

READ_TIMEOUT_SECONDS = 3
# Only these CORSIA performance settings may ever be exposed for a foreign oMLX.
WRITABLE_GLOBAL_SETTINGS = frozenset({"max_concurrent_requests"})
# ...and these while CORSIA manages it.
MANAGED_GLOBAL_SETTINGS = WRITABLE_GLOBAL_SETTINGS | {"prefill_priority"}
# The managed model's settings CORSIA may write; ``max_context_window`` is the recommender's.
MANAGED_MODEL_SETTINGS = frozenset({
    "max_context_window", "max_tokens", "turboquant_kv_enabled", "turboquant_kv_bits",
    "turboquant_skip_last", "mtp_enabled",
})

_observed: dict[str, dict[str, Any]] = {}
# server id -> (oMLX model id, that model's settings as oMLX reported them)
_observed_model: dict[str, tuple[str, dict[str, Any]]] = {}


def _find(settings: dict[str, Any], key: str) -> tuple[bool, Any]:
    """The value for ``key`` at the top level or one group down, as oMLX nests it."""
    if key in settings:
        return True, settings[key]
    for group in settings.values():
        if isinstance(group, dict) and key in group:
            return True, group[key]
    return False, None


def _managed_model() -> LocalModel | None:
    from llm import own_omlx_management

    record = own_omlx_management.read()
    return record.model if record is not None and record.active else None


def writable_global_settings() -> frozenset[str]:
    """The oMLX-wide settings CORSIA may offer to change on the person's own oMLX now."""
    from llm import own_omlx_management

    return MANAGED_GLOBAL_SETTINGS if own_omlx_management.active() else WRITABLE_GLOBAL_SETTINGS


def model_recommendations(model: LocalModel) -> dict[str, Any]:
    """What CORSIA sets on the model it downloaded into the person's oMLX."""
    values = {key: value for key, value in MODEL_SETTINGS.items() if key in MANAGED_MODEL_SETTINGS}
    values["max_context_window"] = model.context_window
    if model.mtp:
        values["mtp_enabled"] = True
    return values


async def _observe_model(server: dict[str, Any], model: LocalModel | None, retain_on_failure: bool) -> bool:
    """Read the managed model's settings; whether this is the first read to find them."""
    if model is None:
        _observed_model.pop(server["id"], None)
        return False
    try:
        result = await asyncio.wait_for(
            get_model_settings({"server_id": server["id"]}, model.model), READ_TIMEOUT_SECONDS,
        )
    except Exception:
        result = {}
    settings = result.get("settings") if result.get("ok") else None
    if isinstance(settings, dict):
        seen = _observed_model.get(server["id"], (None, {}))[0] == model.model
        _observed_model[server["id"]] = (model.model, settings)
        return not seen
    if not retain_on_failure:
        _observed_model.pop(server["id"], None)
    return False


async def _model_arrived() -> None:
    """The managed model is in their oMLX: its settings decision can list every row now."""
    from onboarding.omlx_settings_wake import rearm

    await rearm()


async def record_observation(server: dict[str, Any], *, retain_on_failure: bool = False) -> bool:
    """Read global settings (and a managed model's); optionally retain a known diff when verification fails."""
    result: dict[str, Any] = {}
    try:
        result = await asyncio.wait_for(
            get_settings({"server_id": server["id"]}),
            READ_TIMEOUT_SECONDS,
        )
    except Exception:
        result = {}
    settings = result.get("settings") if result.get("ok") else None
    if isinstance(settings, dict) and settings:
        _observed[server["id"]] = settings
        if await _observe_model(server, await asyncio.to_thread(_managed_model), retain_on_failure):
            await _model_arrived()
        return True
    if not retain_on_failure:
        _observed.pop(server["id"], None)
        _observed_model.pop(server["id"], None)
    return False


def fingerprint(server_id: str, entries: list[dict[str, Any]]) -> str:
    """Bind an apply request to exactly the diff shown to the person."""
    rows = [(row["setting"], row["current"], row["recommended"], *([row["model"]] if "model" in row else []))
            for row in entries]
    rows.sort(key=lambda row: json.dumps(row, default=str))
    encoded = json.dumps([server_id, rows], separators=(",", ":"), sort_keys=True, default=str)
    return hashlib.sha256(encoded.encode()).hexdigest()


def reports(server_id: str, key: str, model: str | None = None) -> bool:
    """Whether the latest read of this server (or of its managed ``model``) named ``key``."""
    if model is not None:
        observed_model = _observed_model.get(server_id)
        return observed_model is not None and observed_model[0] == model and key in observed_model[1]
    observed = _observed.get(server_id)
    return observed is not None and _find(observed, key)[0]


def differences(server_ids: list[str]) -> list[dict[str, Any]] | None:
    """Each recommended setting the person's oMLX holds at another value.

    ``[]`` when everything observed matches; None when none of the servers has an
    observation. A global setting oMLX did not report is skipped, not counted as
    different; a managed model's unset setting is ``current`` None (oMLX's default).
    A model row carries ``model``, the oMLX model id it applies to.
    """
    present = [sid for sid in server_ids if sid in _observed]
    if not present:
        return None
    writable = writable_global_settings()
    result = []
    for key, recommended in OMLX_GLOBAL_SETTINGS.items():
        if key not in writable:
            continue
        found, current = _find(_observed[present[0]], key)
        if found and current != recommended:
            result.append({"setting": key, "current": current, "recommended": recommended})
    model = _managed_model()
    observed_model = _observed_model.get(present[0])
    if model is not None and observed_model is not None and observed_model[0] == model.model:
        for key, recommended in sorted(model_recommendations(model).items()):
            current = observed_model[1].get(key)
            if current != recommended:
                result.append({"setting": key, "current": current, "recommended": recommended,
                               "model": model.model})
    return result
