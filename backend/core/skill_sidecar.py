"""The ``corsia.yaml`` sidecar beside a ``SKILL.md``: pointer rules and file reading.

``SKILL.md`` frontmatter points at the sidecar with
``metadata: {corsia-data: corsia.yaml, corsia-format: '1'}``; the sidecar holds the
handler and workflow data unwrapped (top-level ``handler:`` and ``workflow:``).
``core.skill_validation`` assembles the result; this module owns only what is
specific to the file.
"""

from __future__ import annotations

import hashlib
import json
import stat
from pathlib import Path
from typing import Any, Mapping

import yaml

SIDECAR_FILE = "corsia.yaml"
SIDECAR_FORMAT = "1"
POINTER_KEY = "corsia-data"
FORMAT_KEY = "corsia-format"
# Lessons and captures keep the top-level frontmatter block until they get their own
# context type; a sidecar never carries them.
BLOCK_ONLY_KEYS = ("lesson", "capture")


def installed_digest_part(relative: str, data: bytes) -> bytes | None:
    """Return the canonical digest input for one installed skill file.

    ``origin`` is local adoption metadata rather than skill content, so a sidecar
    with only that mapping does not contribute to the folder digest.
    """
    if relative != SIDECAR_FILE:
        return data
    parsed, errors = parse_sidecar(data.decode("utf-8"))
    if errors or parsed is None:
        raise ValueError(f"invalid {SIDECAR_FILE} for installed digest")
    payload = {key: value for key, value in parsed.items() if key != "origin"}
    if not payload:
        return None
    return json.dumps(
        payload, sort_keys=True, separators=(",", ":"), ensure_ascii=False
    ).encode("utf-8")


def installed_digest(files: Mapping[str, bytes]) -> str:
    """Digest an installed skill folder while excluding mutable origin metadata."""
    digest = hashlib.sha256()
    for relative in sorted(files):
        part = installed_digest_part(relative, files[relative])
        if part is not None:
            digest.update(relative.encode("utf-8") + b"\0" + hashlib.sha256(part).digest())
    return digest.hexdigest()


def pointer_errors(metadata_field: Any) -> tuple[bool, list[str]]:
    """Return whether a ``corsia-data`` pointer is declared, with its errors.

    Format 1 accepts only the literal ``corsia.yaml`` pointer, so a value an adopted
    or imported skill controls can never name another path.
    """
    if not isinstance(metadata_field, dict) or POINTER_KEY not in metadata_field:
        return False, []
    errors: list[str] = []
    if metadata_field[POINTER_KEY] != SIDECAR_FILE:
        errors.append(f"metadata.{POINTER_KEY} must be exactly {SIDECAR_FILE}")
    if metadata_field.get(FORMAT_KEY) != SIDECAR_FORMAT:
        errors.append(
            f"metadata.{FORMAT_KEY} must be {SIDECAR_FORMAT!r}, the supported version; "
            f"got {metadata_field.get(FORMAT_KEY)!r}"
        )
    return True, errors


def parse_sidecar(text: str) -> tuple[dict[str, Any] | None, list[str]]:
    """Parse sidecar text into its mapping, or report why it is not a valid one."""
    try:
        data = yaml.safe_load(text)
    except yaml.YAMLError as exc:
        return None, [f"{SIDECAR_FILE} parse failed: {exc}"]
    if not isinstance(data, dict):
        return None, [f"{SIDECAR_FILE} must be a mapping"]
    errors = [
        f"{key} is not allowed in {SIDECAR_FILE}; it stays in the SKILL.md corsia block"
        for key in BLOCK_ONLY_KEYS
        if key in data
    ]
    return (None if errors else data), errors


def read_sidecar(skill_dir: Path) -> tuple[str | None, str | None]:
    """Read ``corsia.yaml`` beside a skill as ``(text, problem)``.

    ``(None, None)`` means there is no file. A symlink or other non-regular entry is
    a problem (lstat, so a link is never followed), as is unreadable content.
    """
    path = skill_dir / SIDECAR_FILE
    try:
        mode = path.lstat().st_mode
    except FileNotFoundError:
        return None, None
    except OSError as exc:
        return None, f"{SIDECAR_FILE} unreadable: {exc}"
    if not stat.S_ISREG(mode):
        return None, f"{SIDECAR_FILE} must be a regular file, not a symlink or directory"
    try:
        return path.read_text(encoding="utf-8"), None
    except (OSError, UnicodeDecodeError) as exc:
        return None, f"{SIDECAR_FILE} unreadable: {exc}"
