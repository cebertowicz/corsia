"""Model-independent settings for CORSIA's managed oMLX."""

from __future__ import annotations

import json
from pathlib import Path

_PATH = Path(__file__).with_name("local_profiles.json")
_TOP_KEYS = {"omlx_global_settings", "model_settings"}


def load_settings(path: Path = _PATH) -> tuple[dict, dict]:
    """Read the shared settings; model choice belongs to oMLX's recommender."""
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise RuntimeError(f"Local settings {path.name} are unreadable: {exc}") from exc
    if not isinstance(raw, dict) or set(raw) != _TOP_KEYS:
        raise RuntimeError(f"Local settings {path.name} must have exactly the keys {sorted(_TOP_KEYS)}")
    globals_ = raw["omlx_global_settings"]
    model = raw["model_settings"]
    if not isinstance(globals_, dict) or not isinstance(model, dict):
        raise RuntimeError(f"Local settings {path.name} need settings objects")
    return globals_, model


OMLX_GLOBAL_SETTINGS, MODEL_SETTINGS = load_settings()


def format_size(size_bytes: int) -> str:
    """The one display format for byte sizes."""
    return f"{size_bytes / 1024**3:.2f} GiB"
