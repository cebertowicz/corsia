"""Where the first-run classifier download keeps its files, and the sums it checks them with."""

from __future__ import annotations

import hashlib
import json
import logging
import os
import re
import shutil
from pathlib import Path

from core import corrections_classifier as classifier
from core.corrections_asset_pins import PINNED_MANIFESTS
from core.onnx_asset import AssetError, load_pinned_manifest

logger = logging.getLogger(__name__)

CHUNK_BYTES = 1024 * 1024


def sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        while chunk := handle.read(CHUNK_BYTES):
            digest.update(chunk)
    return digest.hexdigest()


def asset_root() -> Path:
    """Return the app-owned location for the corrections asset."""
    overridden = os.environ.get("CORSIA_CORRECTIONS_ASSET_DIR")
    if overridden:
        return Path(overridden)
    from infra.config import STATE_ROOT

    return STATE_ROOT / "assets" / classifier.CORRECTIONS_SPEC.asset_id


_VERSION_LABEL = re.compile(r"v([0-9]+)")


def version_number(label: str) -> int:
    """Order a pinned version label; only ``v<number>`` is accepted."""
    match = _VERSION_LABEL.fullmatch(label)
    if match is None:
        raise AssetError(f"classifier version {label!r} is not of the form v<number>")
    return int(match.group(1))


def newest_pin() -> str:
    """The highest pinned version label, ordered by number (``v10`` is newer than ``v9``)."""
    return max(PINNED_MANIFESTS, key=version_number)


def root_is_newest_pin(root: Path) -> bool:
    """Whether the manifest at ``root`` names the newest source-pinned version."""
    try:
        version = json.loads((root / "manifest.json").read_text(encoding="utf-8"))["version"]
    except (OSError, ValueError, KeyError, TypeError):
        return False
    return version == newest_pin()


def current_asset_verified(root: Path | None = None) -> bool:
    """Whether the installed root is the newest complete source-pinned asset."""
    root = root or asset_root()
    try:
        manifest = load_pinned_manifest(root, classifier.CORRECTIONS_SPEC)
        manifest.verify_files(classifier.CORRECTIONS_SPEC)
    except (AssetError, OSError):
        return False
    return root_is_newest_pin(root)


def staging_dir(root: Path) -> Path:
    """Beside the root, so the final swap is a rename on one filesystem."""
    return root.parent / f".{root.name}.staging-{newest_pin()}"


def previous_dir(root: Path) -> Path:
    return root.parent / f".{root.name}.previous"


def recover_interrupted_install(root: Path | None = None) -> bool:
    """Put the parked version back when a swap died between its two renames."""
    root = root or asset_root()
    previous = previous_dir(root)
    if root.exists() or not previous.is_dir():
        return False
    os.replace(previous, root)
    logger.warning("Restored the previous corrections classifier after an interrupted install")
    return True


def short_of_space(short_bytes: int) -> str:
    """How much more room the person needs to make, rounded up to a tenth of a gigabyte."""
    return f"Needs {-(-short_bytes // 10**8) / 10:.1f} GB more free space."


def free_bytes(path: Path) -> int:
    probe = path
    while not probe.exists() and probe != probe.parent:
        probe = probe.parent
    return shutil.disk_usage(probe).free
