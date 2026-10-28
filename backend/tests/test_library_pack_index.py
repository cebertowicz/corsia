from copy import deepcopy

import pytest

from library.index import LibraryIndexValidationError, _validate_index
from library.items import search_items


def pack_row():
    return {
        "kind": "pack", "pack_kind": "blueprint", "type": "corsia-blueprint",
        "name": "sample-blueprint", "description": "A software project blueprint",
        "version": "1.0.0", "digest": "sha256:" + "b" * 64,
        "source": {"repository": "example/library", "path": "packs/sample-blueprint", "commit": "a" * 40},
        "files": [{"path": "manifest.yaml", "sha256": "a" * 64},
                  {"path": "CHANGELOG.md", "sha256": "b" * 64}],
        "withdrawn": False, "withdrawn_reason": None,
    }


def test_blueprint_is_indexed_and_searchable():
    row = pack_row()
    indexed = _validate_index({"skills": [row]})
    assert search_items(indexed, "software blueprint") == [row]


@pytest.mark.parametrize("path", ["../outside", "/absolute", ".hidden", "inner/.hidden"])
def test_blueprint_refuses_unsafe_indexed_paths(path):
    row = pack_row()
    row["files"].append({"path": path, "sha256": "c" * 64})
    with pytest.raises(LibraryIndexValidationError):
        _validate_index({"skills": [row]})


@pytest.mark.parametrize("name", ["../escape", "a/b", ".hidden", "", "a\\\\b"])
def test_blueprint_refuses_unsafe_names(name):
    row = pack_row()
    row["name"] = name
    with pytest.raises(LibraryIndexValidationError):
        _validate_index({"skills": [row]})


@pytest.mark.parametrize("version", ["../escape", "1", "1.0", "v1.0.0", ".hidden"])
def test_blueprint_requires_semver(version):
    row = pack_row()
    row["version"] = version
    with pytest.raises(LibraryIndexValidationError):
        _validate_index({"skills": [row]})


def test_connector_pack_is_refused_by_name():
    row = deepcopy(pack_row())
    row["pack_kind"] = "connector"
    with pytest.raises(LibraryIndexValidationError, match="connector"):
        _validate_index({"skills": [row]})
