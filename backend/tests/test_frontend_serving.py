"""The frontend route serves files from the build and nothing outside it."""

from __future__ import annotations

import asyncio

import pytest


@pytest.fixture
def build(tmp_path, monkeypatch):
    import main

    dist = tmp_path / "dist"
    (dist / "_app" / "immutable").mkdir(parents=True)
    (dist / "index.html").write_text("app page")
    (dist / "_app" / "immutable" / "entry.js").write_text("asset")
    (tmp_path / "outside.txt").write_text("not part of the build")
    monkeypatch.setattr(main, "FRONTEND_DIR", dist)
    return dist


def _serve(path: str):
    import main

    return asyncio.run(main.serve_frontend(path))


def test_a_file_in_the_build_is_served(build):
    response = _serve("_app/immutable/entry.js")
    assert response.body == b"asset"
    assert response.headers["Cache-Control"] == "public, max-age=31536000, immutable"


@pytest.mark.parametrize("path", [
    "../outside.txt",
    "_app/../../outside.txt",
    "{tmp}/outside.txt",
    "\x00",
    "_app/immutable/entry\x00.js",
])
def test_a_path_outside_the_build_gets_the_app_page(build, path):
    response = _serve(path.format(tmp=build.parent))
    assert response.body == b"app page"


def test_a_link_out_of_the_build_gets_the_app_page(build):
    (build / "linked.txt").symlink_to(build.parent / "outside.txt")
    assert _serve("linked.txt").body == b"app page"


def test_an_io_fault_on_a_build_file_still_raises(build, monkeypatch):
    import pathlib

    def denied(self):
        raise PermissionError(13, "Permission denied", str(self))

    monkeypatch.setattr(pathlib.Path, "is_file", denied)
    with pytest.raises(PermissionError):
        _serve("_app/immutable/entry.js")
