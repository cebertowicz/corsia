"""Cached revision hashes for document image pins."""

from functools import lru_cache
from pathlib import Path

from registry.identity_metadata import sha256_file


def image_revision(path: Path) -> str:
    """The revision a pin is placed on: the image file's SHA-256, hashed once per version of the file.

    Every comment read judges each commented image's staleness (FR13.9), so a
    file whose identity, size and times are unchanged keeps its last hash: a
    stat per image per read, not a full read.
    """
    st = path.stat()
    return _file_sha256(str(path), st.st_dev, st.st_ino, st.st_size, st.st_mtime_ns, st.st_ctime_ns)


@lru_cache(maxsize=512)
def _file_sha256(path: str, *version: int) -> str:
    return sha256_file(Path(path))
