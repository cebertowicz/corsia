"""Verified selection of the installed corrections-classifier asset version."""

from __future__ import annotations

import re
from pathlib import Path

from core.corrections_classifier import ASSET_ID, AssetError, AssetManifest, asset_root, load_pinned_manifest

_VERSION_LABEL = re.compile(r"v([0-9]+)")


def version_number(label: str) -> int:
    """Order a pinned version label; only ``v<number>`` is accepted."""
    match = _VERSION_LABEL.fullmatch(label)
    if match is None:
        raise AssetError(f"classifier version {label!r} is not of the form v<number>")
    return int(match.group(1))


def candidate_roots() -> list[Path]:
    """Return only declared asset locations, newest pin first after the installed root."""
    from core import corrections_asset_pins

    root = asset_root()
    siblings = (
        root.parent / f"{ASSET_ID}-{version}"
        for version in sorted(corrections_asset_pins.PINNED_MANIFESTS, key=version_number, reverse=True)
    )
    return [root, *(path for path in siblings if path != root)]


def installed() -> bool:
    """Whether any declared asset location contains a manifest."""
    return any((path / "manifest.json").is_file() for path in candidate_roots())


def select_manifest() -> tuple[AssetManifest, list[dict[str, str | None]]]:
    """Return the highest verified pinned asset and each present candidate's state."""
    verified: list[tuple[int, int, AssetManifest]] = []
    versions: list[dict[str, str | None]] = []
    for order, directory in enumerate(candidate_roots()):
        if not (directory / "manifest.json").is_file():
            continue
        try:
            manifest = load_pinned_manifest(directory)
            manifest.verify_files()
            number = version_number(manifest.version)
        except (AssetError, OSError) as error:
            versions.append({"location": directory.name, "version": None, "state": "failed", "reason": str(error)})
            continue
        verified.append((number, -order, manifest))
        versions.append({"location": directory.name, "version": manifest.version, "state": "ready", "reason": None})
    if verified:
        return max(verified, key=lambda item: item[:2])[2], versions
    if not versions:
        raise AssetError("classifier asset is absent")
    raise AssetError(f"no verified corrections classifier asset: {versions[0]['reason']}")
