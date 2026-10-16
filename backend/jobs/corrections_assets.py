"""Verified selection of the installed corrections-classifier asset version."""

from __future__ import annotations

import os
import re
from collections.abc import Mapping
from pathlib import Path

from core import corrections_classifier
from core.onnx_asset import AssetError, AssetManifest, load_pinned_manifest

_VERSION_LABEL = re.compile(r"v([0-9]+)")


class AssetSelectionError(AssetError):
    """Selection failed after inspecting every installed candidate."""

    def __init__(
        self,
        message: str,
        versions: list[dict[str, str | None]],
        *,
        reader: str | None = None,
    ) -> None:
        super().__init__(message, reader=reader)
        self.versions = versions


def asset_root() -> Path:
    """Return the app-owned location for the corrections asset."""
    overridden = os.environ.get("CORSIA_CORRECTIONS_ASSET_DIR")
    if overridden:
        return Path(overridden)
    from infra.config import STATE_ROOT

    return STATE_ROOT / "assets" / corrections_classifier.CORRECTIONS_SPEC.asset_id


def version_number(label: str) -> int:
    """Order a pinned version label; only ``v<number>`` is accepted."""
    match = _VERSION_LABEL.fullmatch(label)
    if match is None:
        raise AssetError(f"classifier version {label!r} is not of the form v<number>")
    return int(match.group(1))


def candidate_roots() -> list[Path]:
    """Return only declared asset locations, newest pin first after the installed root."""
    root = asset_root()
    asset_id = corrections_classifier.CORRECTIONS_SPEC.asset_id
    siblings = (
        root.parent / f"{asset_id}-{version}"
        for version in sorted(corrections_classifier.CORRECTIONS_SPEC.pins, key=version_number, reverse=True)
    )
    return [root, *(path for path in siblings if path != root)]


def installed() -> bool:
    """Whether any declared asset location contains a manifest."""
    return any((path / "manifest.json").is_file() for path in candidate_roots())


def select_manifest(*, exclude: Mapping[str, str] | None = None) -> tuple[AssetManifest, list[dict[str, str | None]]]:
    """Return the highest verified non-excluded pin and diagnostics for every candidate."""
    exclude = exclude or {}
    verified: list[tuple[int, int, AssetManifest]] = []
    versions: list[dict[str, str | None]] = []
    readers: list[str] = []
    for order, directory in enumerate(candidate_roots()):
        if not (directory / "manifest.json").is_file():
            continue
        try:
            manifest = load_pinned_manifest(directory, corrections_classifier.CORRECTIONS_SPEC)
            manifest.verify_files(corrections_classifier.CORRECTIONS_SPEC)
            number = version_number(manifest.version)
        except (AssetError, OSError) as error:
            versions.append({"location": directory.name, "version": None, "state": "failed", "reason": str(error)})
            if isinstance(error, AssetError) and error.reader:
                readers.append(error.reader)
            continue
        reason = exclude.get(manifest.version)
        if reason is not None:
            versions.append({"location": directory.name, "version": manifest.version, "state": "failed", "reason": reason})
            continue
        verified.append((number, -order, manifest))
        versions.append({"location": directory.name, "version": manifest.version, "state": "ready", "reason": None})
    if verified:
        return max(verified, key=lambda item: item[:2])[2], versions
    if not versions:
        raise AssetSelectionError(
            "classifier asset is absent", [],
            reader="The corrections classifier is not installed. Retry setup.",
        )
    raise AssetSelectionError(
        f"no verified corrections classifier asset: {versions[0]['reason']}",
        versions,
        reader=readers[0] if readers else "The corrections classifier download is damaged. Retry setup.",
    )
