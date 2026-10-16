"""Where the first-run classifier download keeps its files, and the sums it checks them with."""

from __future__ import annotations

import hashlib
import logging
import os
import shutil
from pathlib import Path

from core import corrections_classifier as classifier
from core.corrections_asset_pins import PINNED_MANIFESTS

logger = logging.getLogger(__name__)

CHUNK_BYTES = 1024 * 1024


def sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        while chunk := handle.read(CHUNK_BYTES):
            digest.update(chunk)
    return digest.hexdigest()


def staging_dir(root: Path) -> Path:
    """Beside the root, so the final swap is a rename on one filesystem."""
    return root.parent / f".{root.name}.staging-{max(PINNED_MANIFESTS)}"


def previous_dir(root: Path) -> Path:
    return root.parent / f".{root.name}.previous"


def recover_interrupted_install(root: Path | None = None) -> bool:
    """Put the parked version back when a swap died between its two renames."""
    root = root or classifier.asset_root()
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
