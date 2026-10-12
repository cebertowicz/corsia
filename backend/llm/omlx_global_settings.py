"""oMLX's server-wide settings against CORSIA's recommended ones, read once per discovery.

The read rides oMLX's existing catalog discovery and is kept until the next
discovery of that server, so a model-sources read never calls oMLX. A failed or
empty read stores nothing: the difference is then unknown, never an error.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
from typing import Any

from llm.local_profiles import OMLX_GLOBAL_SETTINGS
from llm.providers.omlx_settings import get_settings

READ_TIMEOUT_SECONDS = 3
# Only these CORSIA performance settings may ever be exposed for a foreign oMLX.
WRITABLE_GLOBAL_SETTINGS = frozenset({"max_concurrent_requests"})

_observed: dict[str, dict[str, Any]] = {}


def _find(settings: dict[str, Any], key: str) -> tuple[bool, Any]:
    """The value for ``key`` at the top level or one group down, as oMLX nests it."""
    if key in settings:
        return True, settings[key]
    for group in settings.values():
        if isinstance(group, dict) and key in group:
            return True, group[key]
    return False, None


async def record_observation(server: dict[str, Any], *, retain_on_failure: bool = False) -> bool:
    """Read global settings; optionally retain a known diff when verification fails."""
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
        return True
    if not retain_on_failure:
        _observed.pop(server["id"], None)
    return False


def fingerprint(server_id: str, entries: list[dict[str, Any]]) -> str:
    """Bind an apply request to exactly the diff shown to the person."""
    triples = sorted((row["setting"], row["current"], row["recommended"]) for row in entries)
    encoded = json.dumps([server_id, triples], separators=(",", ":"), sort_keys=True, default=str)
    return hashlib.sha256(encoded.encode()).hexdigest()


def reports(server_id: str, key: str) -> bool:
    """Whether the latest read of this server named ``key``."""
    observed = _observed.get(server_id)
    return observed is not None and _find(observed, key)[0]


def differences(server_ids: list[str]) -> list[dict[str, Any]] | None:
    """Each recommended setting the person's oMLX holds at another value.

    ``[]`` when everything observed matches; None when none of the servers has an
    observation. A setting oMLX did not report is skipped, not counted as different.
    """
    observed = [_observed[sid] for sid in server_ids if sid in _observed]
    if not observed:
        return None
    result = []
    for key, recommended in OMLX_GLOBAL_SETTINGS.items():
        if key not in WRITABLE_GLOBAL_SETTINGS:
            continue
        found, current = _find(observed[0], key)
        if found and current != recommended:
            result.append({"setting": key, "current": current, "recommended": recommended})
    return result
