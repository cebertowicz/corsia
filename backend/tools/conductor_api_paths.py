"""Preserve protected-file boundaries when an API accepts a path."""
from __future__ import annotations

from pathlib import Path
from typing import Any

from tools.host_path_policy import ordinary_host_path_denial


def forbidden_path(value: Any, *, for_write: bool) -> bool:
    if isinstance(value, list):
        return any(forbidden_path(item, for_write=for_write) for item in value)
    if not isinstance(value, dict):
        return False
    for key, item in value.items():
        if isinstance(item, str) and (key == "path" or key.endswith("_path") or key == "directory"):
            path = Path(item).expanduser().resolve()
            if ordinary_host_path_denial(path, for_write=for_write):
                return True
            if path.name.startswith(".env") or path.suffix in {".pem", ".key", ".p12"}:
                return True
            if for_write and (
                {".corsia", "context"}.issubset(path.parts) or {".corsia", "skills"}.issubset(path.parts)
            ):
                return True
        if forbidden_path(item, for_write=for_write):
            return True
    return False
