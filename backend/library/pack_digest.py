"""Raw folder digest for a library pack, independent of skill sidecars."""

from __future__ import annotations

import hashlib
from collections.abc import Mapping


def pack_digest(files: Mapping[str, bytes]) -> str:
    """Hash UTF-8 paths and their raw SHA-256 bytes in path-byte order."""
    digest = hashlib.sha256()
    for path in sorted(files, key=lambda name: name.encode("utf-8")):
        digest.update(path.encode("utf-8") + b"\0" + hashlib.sha256(files[path]).digest())
    return digest.hexdigest()
