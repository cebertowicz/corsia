#!/usr/bin/env python3
"""Re-vendor the baked hosted-catalog snapshot from the corsia-catalog repo.

Copies the curated catalog data CORSIA consumes into the in-repo baked snapshot
(``backend/llm/hosted_catalog/snapshot/``). This is the **baked-snapshot** half of
the catalog-delivery contract (HMC2); remote-fetch/update is a later slice.

Today it vendors the ``cloud-models`` section + the snapshot VERSION; new sections
(``connectors``, ``model-profiles``) are added to ``SECTIONS`` as they land — the
copy logic is section-agnostic.

Source repo location (config, not hardcoded): ``$CORSIA_CATALOG_REPO`` or the
``--source`` flag, defaulting to ``~/Documents/projects/corsia-catalog``. Fails
loud if the source is absent.
"""

from __future__ import annotations

import argparse
import os
import shutil
import sys
from pathlib import Path

# Catalog sections (subdirs of the repo's `catalog/`) vendored into the snapshot.
# Extend as connectors / model-profiles are consumed — copy logic is generic.
SECTIONS = ["cloud-models", "connectors"]

_REPO_ROOT = Path(__file__).resolve().parents[1]
_SNAPSHOT_DIR = _REPO_ROOT / "backend" / "llm" / "hosted_catalog" / "snapshot"
_DEFAULT_SOURCE = Path.home() / "Documents" / "projects" / "corsia-catalog"


def _resolve_source(cli_source: str | None) -> Path:
    """Resolve the corsia-catalog repo path from --source / env / default; fail loud."""
    raw = cli_source or os.environ.get("CORSIA_CATALOG_REPO") or str(_DEFAULT_SOURCE)
    source = Path(raw).expanduser()
    if not (source / "catalog").is_dir():
        sys.exit(
            f"corsia-catalog repo not found at {source} (no catalog/ dir). "
            "Clone it or pass --source / set CORSIA_CATALOG_REPO."
        )
    return source


def _sync_section(source: Path, section: str) -> int:
    """Mirror one catalog section's *.json into the snapshot; return file count."""
    src_dir = source / "catalog" / section
    if not src_dir.is_dir():
        sys.exit(f"section '{section}' missing from source at {src_dir}.")
    dst_dir = _SNAPSHOT_DIR / section
    if dst_dir.exists():
        shutil.rmtree(dst_dir)
    dst_dir.mkdir(parents=True)
    files = sorted(src_dir.glob("*.json"))
    for path in files:
        shutil.copy2(path, dst_dir / path.name)
    return len(files)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source", help="path to the corsia-catalog repo")
    args = parser.parse_args()
    source = _resolve_source(args.source)

    _SNAPSHOT_DIR.mkdir(parents=True, exist_ok=True)
    for section in SECTIONS:
        count = _sync_section(source, section)
        print(f"  {section}: {count} file(s)")

    version_src = source / "snapshot" / "VERSION.json"
    if version_src.is_file():
        shutil.copy2(version_src, _SNAPSHOT_DIR / "VERSION.json")
        print("  VERSION.json copied")

    print(f"Vendored hosted-catalog snapshot -> {_SNAPSHOT_DIR}")


if __name__ == "__main__":
    main()
