"""Canonical file-write receipt shared with the result observer."""

from pathlib import Path
import re

_WRITE_SUCCESS = re.compile(
    r"^Successfully wrote [0-9]+ bytes to (?P<path>.+)$",
    re.DOTALL,
)


def write_success_result(path: Path, size: int) -> str:
    """Return the canonical native write receipt consumed by the observer."""
    return f"Successfully wrote {size} bytes to {path}"


def write_success_path(result: str) -> Path | None:
    """Read the effective absolute path from a canonical native write receipt."""
    match = _WRITE_SUCCESS.fullmatch(result)
    if match is None:
        return None
    path = Path(match.group("path")).expanduser()
    return path if path.is_absolute() else None
