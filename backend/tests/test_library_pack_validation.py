import hashlib

import pytest

from library.items import LibraryIntegrityError
from library.pack_digest import pack_digest
from library.pack_validation import validate_pack


def file_row(path, data):
    return {"path": path, "sha256": hashlib.sha256(data).hexdigest()}


def bundle():
    files = {"manifest.yaml": b"schema: corsia.pack/1\nkind: blueprint\nname: demo\nversion: 1.0.0\n", "CHANGELOG.md": b"# Changes\n"}
    item = {"name": "demo", "version": "1.0.0", "files": [file_row(p, data) for p, data in files.items()], "digest": pack_digest(files)}
    return item, files


def test_valid_pack():
    item, files = bundle()
    validate_pack(item, files)


@pytest.mark.parametrize("path,content", [(".hidden", b"text"), ("scripts/run", b"text"), ("readme", b"\x00"), ("assets/evil.png", b"\x00text")])
def test_forbidden_contents(path, content):
    item, files = bundle()
    files[path] = content
    item["files"].append(file_row(path, content))
    item["digest"] = pack_digest(files)
    with pytest.raises(LibraryIntegrityError):
        validate_pack(item, files)


def test_recognized_binary_asset_is_permitted():
    item, files = bundle()
    files["assets/example.png"] = b"\x89PNG\r\n\x1a\n\x00image"
    item["files"].append(file_row("assets/example.png", files["assets/example.png"]))
    item["digest"] = pack_digest(files)
    validate_pack(item, files)


def test_digest_and_manifest_mismatch():
    item, files = bundle()
    item["digest"] = "0" * 64
    with pytest.raises(LibraryIntegrityError, match="digest"):
        validate_pack(item, files)
    item["digest"] = pack_digest(files)
    item["version"] = "2.0.0"
    with pytest.raises(LibraryIntegrityError, match="manifest"):
        validate_pack(item, files)
