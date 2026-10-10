#!/usr/bin/env python3
"""Generate or verify the packaged System-tier manifest."""

from __future__ import annotations

import argparse
import hashlib
import importlib.util
import json
import sys
from pathlib import Path


def _load_locator():
    """Load the leaf locator module by file; the ``infra`` package needs a workspace."""
    path = Path(__file__).resolve().parents[1] / "backend/infra/system_package.py"
    spec = importlib.util.spec_from_file_location("_system_package_locator", path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


_LOCATOR = _load_locator()
COORDINATION_MODELS_ROOT = _LOCATOR.COORDINATION_MODELS_ROOT
coordination_models_dir = _LOCATOR.coordination_models_dir
ROOT = _LOCATOR.system_package_dir()
MANIFEST = ROOT / "MANIFEST.json"


def _kind_and_role(relative: Path) -> tuple[str, str | None]:
    """Classify one System resource path for loader selection."""
    if relative.parts[0] == COORDINATION_MODELS_ROOT:
        return "coordination-model", None
    if relative.parts[0] == "skills":
        return "skill", None
    if relative.parts[0] != "context":
        raise ValueError(f"unsupported System resource {relative}")
    role = relative.parts[2] if len(relative.parts) > 2 and relative.parts[1] == "roles" else None
    return "context", role


def _declared_files() -> list[tuple[Path, Path]]:
    """Return every (manifest-relative path, source file) the manifest declares."""
    models = coordination_models_dir()
    found = [
        (path.relative_to(ROOT), path)
        for path in ROOT.rglob("*")
        if path.is_file() and path.name != "MANIFEST.json"
    ]
    found += [
        (Path(COORDINATION_MODELS_ROOT) / path.relative_to(models), path)
        for path in models.rglob("*")
        if path.is_file()
    ]
    return sorted(found, key=lambda item: item[0])


def manifest_payload() -> dict[str, object]:
    """Build the deterministic manifest payload from packaged resources."""
    files = []
    for relative, path in _declared_files():
        kind, role = _kind_and_role(relative)
        raw = path.read_bytes()
        item: dict[str, object] = {
            "path": relative.as_posix(),
            "sha256": hashlib.sha256(raw).hexdigest(),
            "bytes": len(raw),
            "kind": kind,
        }
        if role is not None:
            item["role"] = role
        files.append(item)
    return {"version": "v1", "files": files}


def main() -> int:
    """Write the manifest, or return failure when --check finds drift."""
    parser = argparse.ArgumentParser()
    parser.add_argument("--check", action="store_true")
    args = parser.parse_args()
    rendered = json.dumps(manifest_payload(), indent=2, sort_keys=True) + "\n"
    if args.check:
        if MANIFEST.read_text(encoding="utf-8") == rendered:
            return 0
        print("System manifest is stale; run python scripts/generate_system_manifest.py",
              file=sys.stderr)
        return 1
    MANIFEST.write_text(rendered, encoding="utf-8")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
