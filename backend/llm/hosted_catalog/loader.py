"""General boot-safe reader for the vendored hosted-catalog snapshot.

One reusable pattern for every catalog row class: cloud-model reasoning tables
today; connectors and model-profiles next, with no change to this module. A
"section" is a directory of ``.json`` files under ``snapshot/`` (e.g.
``cloud-models/``); :func:`load_section` returns its files keyed by filename stem.

Boot-safety (HMC2): nothing reads at import; the first :func:`load_section` call
reads + parses + caches. A missing section dir or an unparseable file raises
:class:`CatalogSnapshotError` **at use** with repair guidance — it never crashes
startup and never silently returns empty (a corrupt snapshot must fail loud, not
degrade into wrong model truth — HMC11).
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

_SNAPSHOT_DIR = Path(__file__).parent / "snapshot"

# section name -> {file stem: parsed json}. Populated lazily, never at import.
_CACHE: dict[str, dict[str, Any]] = {}

_REPAIR = (
    "The vendored hosted-catalog snapshot is missing or corrupt. Re-vendor it from "
    "the corsia-catalog repo: `python scripts/sync_catalog_snapshot.py`."
)


class CatalogSnapshotError(RuntimeError):
    """The vendored snapshot is missing or unparseable. Always carries repair guidance."""


def load_section(section: str) -> dict[str, Any]:
    """Return one snapshot section as ``{file_stem: parsed_json}``.

    Lazy + cached: the section is read once and memoised. Reusable verbatim by
    any row class (cloud-models, connectors, model-profiles).

    Raises:
        CatalogSnapshotError: the section dir is absent, or a file in it does not
            parse — fails loud (with repair guidance) rather than returning empty,
            so a corrupt snapshot can't silently produce wrong model truth.
    """
    cached = _CACHE.get(section)
    if cached is not None:
        return cached

    section_dir = _SNAPSHOT_DIR / section
    if not section_dir.is_dir():
        raise CatalogSnapshotError(
            f"hosted-catalog section '{section}' not found at {section_dir}. {_REPAIR}"
        )

    out: dict[str, Any] = {}
    for path in sorted(section_dir.glob("*.json")):
        try:
            out[path.stem] = json.loads(path.read_text())
        except (OSError, ValueError) as e:
            raise CatalogSnapshotError(
                f"hosted-catalog file {path} is unreadable/unparseable: {e}. {_REPAIR}"
            ) from e

    _CACHE[section] = out
    return out


def snapshot_version() -> str:
    """Return the vendored snapshot version (provenance, HMC17); 'unknown' if absent.

    Provenance is best-effort metadata, not routing-critical, so a missing
    VERSION.json degrades to 'unknown' rather than failing loud.
    """
    try:
        meta = json.loads((_SNAPSHOT_DIR / "VERSION.json").read_text())
        return str(meta.get("snapshot_version", "unknown"))
    except (OSError, ValueError):
        return "unknown"


def _reset_cache_for_tests() -> None:
    """Clear the memoised sections (test-only — exercise the lazy read path)."""
    _CACHE.clear()
