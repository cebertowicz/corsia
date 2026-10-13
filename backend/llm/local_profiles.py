"""The local model profiles setup can install: one data file, one validating loader.

``local_profiles.json`` lists every profile in ascending RAM order. The first is
the profile for the lowest RAM band and the one setup installs when none is
named. oMLX's global settings belong to the server, so they are stored once;
per-model settings are shared defaults plus each profile's own overrides.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path

_PATH = Path(__file__).with_name("local_profiles.json")


@dataclass(frozen=True)
class LocalProfile:
    """One installable local model and its per-model oMLX settings; sizes are in bytes.

    ``sized_at_revision`` is the Hugging Face revision whose files the sizes were
    read from. oMLX's download request takes only ``repo_id`` and ``hf_token``
    (``omlx/admin/routes.py`` ``HFDownloadRequest``), so the download fetches the
    repository's current head; the revision records what was sized, not what is pinned.
    """

    id: str
    display_name: str
    min_ram_gib: int
    model: str
    repository: str
    reasoning_effort_levels: list[str]
    sized_at_revision: str
    download_size_bytes: int
    resident_weight_bytes: int
    kv_mb_per_token: float
    kv_layer_note: str
    min_context: int
    model_settings: dict


_TOP_KEYS = {"omlx_global_settings", "model_settings", "profiles"}
_PROFILE_TYPES: dict[str, type | tuple[type, ...]] = {
    "id": str, "display_name": str, "min_ram_gib": int, "model": str, "repository": str,
    "reasoning_effort_levels": list, "sized_at_revision": str, "download_size_bytes": int,
    "resident_weight_bytes": int,
    "kv_mb_per_token": (int, float), "kv_layer_note": str, "min_context": int, "model_settings": dict,
}
# Values that may be empty or zero: the first band starts at 0 GiB and a profile
# may override no shared model setting.
_MAY_BE_EMPTY = {"min_ram_gib", "kv_layer_note", "model_settings"}


def _profile(raw: object, shared: dict, name: str) -> LocalProfile:
    if not isinstance(raw, dict) or set(raw) != set(_PROFILE_TYPES):
        raise RuntimeError(f"Local profile in {name} must have exactly the keys {sorted(_PROFILE_TYPES)}")
    for key, expected in _PROFILE_TYPES.items():
        value = raw[key]
        if isinstance(value, bool) or not isinstance(value, expected) or (
            not value and key not in _MAY_BE_EMPTY
        ):
            raise RuntimeError(f"Local profile {raw.get('id')!r} in {name}: invalid value for {key}")
    levels = raw["reasoning_effort_levels"]
    if not all(isinstance(level, str) and level for level in levels):
        raise RuntimeError(f"Local profile {raw.get('id')!r} in {name}: invalid reasoning effort levels")
    return LocalProfile(**{**raw, "model_settings": {**shared, **raw["model_settings"]}})


def load_profiles(path: Path = _PATH) -> tuple[dict, tuple[LocalProfile, ...]]:
    """Read oMLX's global settings and every profile, failing loudly on a wrong file.

    Profiles must have unique ids and strictly ascending ``min_ram_gib`` starting at 0.
    """
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise RuntimeError(f"Local profiles {path.name} are unreadable: {exc}") from exc
    if not isinstance(raw, dict) or set(raw) != _TOP_KEYS:
        raise RuntimeError(f"Local profiles {path.name} must have exactly the keys {sorted(_TOP_KEYS)}")
    shared, settings, items = raw["model_settings"], raw["omlx_global_settings"], raw["profiles"]
    if not isinstance(shared, dict) or not isinstance(settings, dict) or not isinstance(items, list) or not items:
        raise RuntimeError(f"Local profiles {path.name} need settings objects and a non-empty profile list")
    profiles = tuple(_profile(item, shared, path.name) for item in items)
    ids = [p.id for p in profiles]
    rams = [p.min_ram_gib for p in profiles]
    if len(set(ids)) != len(ids) or rams[0] != 0 or any(a >= b for a, b in zip(rams, rams[1:])):
        raise RuntimeError(f"Local profiles {path.name} need unique ids and ascending min_ram_gib from 0")
    return settings, profiles


OMLX_GLOBAL_SETTINGS, PROFILES = load_profiles()
# The lowest RAM band: what setup installs when no profile is named.
DEFAULT_PROFILE = PROFILES[0]


def profile_by_id(profile_id: str) -> LocalProfile | None:
    """The profile with this id, else None."""
    return next((p for p in PROFILES if p.id == profile_id), None)


def reasoning_effort_levels_for(model: str) -> list[str] | None:
    """Pinned template levels for a configured local profile, if it has one."""
    profile = next((p for p in PROFILES if p.model == model), None)
    return list(profile.reasoning_effort_levels) if profile else None


def setup_command(profile_id: str | None) -> str:
    """The one command that sets up, resumes or retries one profile; the default needs no flag."""
    if profile_id is None or profile_id == DEFAULT_PROFILE.id:
        return "corsia local setup"
    return f"corsia local setup --profile {profile_id}"


def format_size(size_bytes: int) -> str:
    """The one display format for byte sizes."""
    return f"{size_bytes / 1024**3:.2f} GiB"
