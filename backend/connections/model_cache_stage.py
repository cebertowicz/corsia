"""Stage nonsecret model-cache bytes; publish them only once their receipt commits."""
from __future__ import annotations

from dataclasses import dataclass
import hashlib
import os
from pathlib import Path


def file_identity(path: Path) -> tuple[int, int, int] | None:
    try:
        info = path.lstat()
    except FileNotFoundError:
        return None
    if path.is_symlink() or not path.is_file():
        raise RuntimeError('model_cache_path_changed')
    return info.st_ino, info.st_size, info.st_mtime_ns


@dataclass
class ModelCacheStage:
    """A fresh cache file waiting beside its persistent path.

    A same-directory rename keeps the inode, size and mtime, so the receipt is
    known before publication: the account writer commits it first, and the file
    is renamed into place only afterwards (see account_discovery_commit). A
    reader holding an older receipt therefore never validates the new file.
    """
    fresh: Path
    persistent: Path
    cache_id: str
    expected: tuple[int, int, int] | None

    @classmethod
    def prepare(cls, fresh: Path, persistent: Path):
        persistent.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        cache_id = hashlib.sha256(fresh.read_bytes()).hexdigest()
        fresh.chmod(0o600)
        return cls(fresh, persistent, cache_id, file_identity(persistent))

    def receipt(self) -> dict:
        identity = file_identity(self.fresh)
        if identity is None:
            raise RuntimeError('model_cache_path_changed')
        return {'cache_id': self.cache_id, 'path': str(self.persistent), 'file_identity': list(identity)}

    def promote(self) -> dict:
        receipt = self.receipt()
        if file_identity(self.persistent) != self.expected:
            raise RuntimeError('model_cache_path_changed')
        os.replace(self.fresh, self.persistent)
        return receipt


def validate_cache(receipt: dict, path: Path) -> bool:
    """Digest outside the writer; the caller rechecks receipt/stat inside admission."""
    try:
        before = file_identity(path)
        if list(before or ()) != receipt.get('file_identity') or str(path) != receipt.get('path'):
            return False
        return (hashlib.sha256(path.read_bytes()).hexdigest() == receipt.get('cache_id')
                and file_identity(path) == before)
    except (OSError, RuntimeError):
        return False
