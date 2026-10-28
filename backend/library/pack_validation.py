"""Validate the bytes of a fetched blueprint before publishing a copy."""

from __future__ import annotations

import hashlib
from typing import Any

import yaml

from core.media_sniffing import image_mime_from_prefix
from library.items import LibraryIntegrityError, validate_relative_path
from library.pack_digest import pack_digest

_FONT_PREFIXES = (b"\x00\x01\x00\x00", b"OTTO", b"ttcf", b"wOFF", b"wOF2")


def validate_pack(item: dict[str, Any], files: dict[str, bytes]) -> None:
    """Refuse unsafe contents or an envelope inconsistent with the indexed pack."""
    expected_paths = {row["path"] for row in item["files"]}
    if set(files) != expected_paths:
        raise LibraryIntegrityError("pack files do not match the index")
    for row in item["files"]:
        actual = hashlib.sha256(files[row["path"]]).hexdigest()
        if actual != row["sha256"].removeprefix("sha256:").casefold():
            raise LibraryIntegrityError(f"integrity check failed for {row['path']}")
    for path, data in files.items():
        validate_relative_path(path)
        parts = path.split("/")
        if any(part.startswith(".") for part in parts) or "scripts" in parts:
            raise LibraryIntegrityError(f"pack path is not allowed: {path}")
        try:
            data.decode("utf-8")
            binary = b"\0" in data
        except UnicodeDecodeError:
            binary = True
        if binary and not (
            parts[0] == "assets" and len(parts) > 1
            and (image_mime_from_prefix(data) or data.startswith(_FONT_PREFIXES))
        ):
            raise LibraryIntegrityError(f"unsupported binary content: {path}")
    if pack_digest(files) != item["digest"].removeprefix("sha256:").casefold():
        raise LibraryIntegrityError("pack digest mismatch")
    try:
        manifest = yaml.safe_load(files["manifest.yaml"].decode("utf-8"))
    except (KeyError, UnicodeDecodeError, yaml.YAMLError) as error:
        raise LibraryIntegrityError("invalid pack manifest") from error
    if not isinstance(manifest, dict) or any(
        manifest.get(key) != expected
        for key, expected in (
            ("schema", "corsia.pack/1"), ("kind", "blueprint"),
            ("name", item["name"]), ("version", item["version"]),
        )
    ):
        raise LibraryIntegrityError("pack manifest does not match indexed blueprint")
