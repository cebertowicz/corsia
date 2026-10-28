"""Publish verified blueprint bytes into a thread without replacing a copy."""

from __future__ import annotations

import os
from pathlib import Path
import shutil
import tempfile

from core.workspace_scaffolding import atomic_rename_noreplace


def publish_pack(thread_root: Path, name: str, version: str, files: dict[str, bytes]) -> Path:
    """Publish a validated pack via a sibling stage; callers validate before this call."""
    parent = thread_root
    for part in ("packs", name):
        parent = parent / part
        if parent.is_symlink():
            raise ValueError("pack destination contains a symlink")
        parent.mkdir(exist_ok=True)
        if not parent.is_dir():
            raise ValueError("pack destination is not a directory")
    stage = Path(tempfile.mkdtemp(prefix=".pack-", dir=parent))
    try:
        for relative, data in files.items():
            target = stage / relative
            target.parent.mkdir(parents=True, exist_ok=True)
            with target.open("xb") as handle:
                handle.write(data)
            target.chmod(0o444)
        for root, directories, _names in os.walk(stage, topdown=False):
            for directory in directories:
                (Path(root) / directory).chmod(0o555)
        stage.chmod(0o555)
        # Rename is atomic and explicitly refuses an existing destination.
        atomic_rename_noreplace(stage, parent / version)
    finally:
        if stage.exists():
            for root, directories, _names in os.walk(stage):
                Path(root).chmod(0o755)
                for directory in directories:
                    (Path(root) / directory).chmod(0o755)
            shutil.rmtree(stage)
    return parent / version
