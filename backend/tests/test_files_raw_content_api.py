"""``GET /api/files/content?raw=true`` serves a workspace image's bytes."""

from __future__ import annotations

import base64
import os
import sys
import uuid

import pytest
from auth_fixtures import OWNER_TOKEN

sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))

pytestmark = pytest.mark.integration

_AUTH = "?token=" + OWNER_TOKEN
_PNG = base64.b64decode(
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR4nGP4z8DwHwAFAAH/q842iQAAAABJRU5ErkJggg=="
)
_SVG = b'<svg xmlns="http://www.w3.org/2000/svg" width="4" height="4"><script>1</script></svg>'


@pytest.fixture
def image_file(integration_workspace):
    made = []

    def make(suffix: str, data: bytes):
        p = integration_workspace / f"raw-{uuid.uuid4().hex[:8]}{suffix}"
        p.write_bytes(data)
        made.append(p)
        return p

    yield make
    for p in made:
        p.unlink(missing_ok=True)


def _raw(client, path, auth=_AUTH):
    return client.get("/api/files/content" + auth + f"&path={path}&raw=true")


def test_png_is_served_as_bytes_with_image_content_type(integration_client, image_file):
    p = image_file(".png", _PNG)
    r = _raw(integration_client, p)
    assert r.status_code == 200, r.text
    assert r.headers["content-type"] == "image/png"
    assert r.headers["x-content-type-options"] == "nosniff"
    assert r.content == _PNG


def test_svg_is_served_as_svg_with_a_locked_down_policy(integration_client, image_file):
    p = image_file(".svg", _SVG)
    r = _raw(integration_client, p)
    assert r.status_code == 200, r.text
    assert r.headers["content-type"].startswith("image/svg+xml")
    assert "sandbox" in r.headers["content-security-policy"]


def test_uppercase_extension_is_served(integration_client, image_file):
    p = image_file(".GIF", b"GIF89a")
    assert _raw(integration_client, p).headers["content-type"] == "image/gif"


def test_non_image_file_is_refused(integration_client, image_file):
    p = image_file(".py", b"print(1)\n")
    assert _raw(integration_client, p).status_code == 415


def test_bad_token_is_refused(integration_client, image_file):
    p = image_file(".png", _PNG)
    assert _raw(integration_client, p, "?token=not-a-token").status_code == 401


def test_path_outside_scope_is_refused(integration_client, tmp_path):
    outside = tmp_path / "out.png"
    outside.write_bytes(_PNG)
    assert _raw(integration_client, outside).status_code == 400


def test_raw_read_does_not_register_or_notify(integration_client, image_file):
    from registry import service as registry_service

    p = image_file(".png", _PNG)
    seen = []
    original = registry_service.notify_changed
    registry_service.notify_changed = lambda n: seen.append(n)
    try:
        assert _raw(integration_client, p).status_code == 200
    finally:
        registry_service.notify_changed = original
    assert seen == []


def test_revision_probe_accepts_an_image_larger_than_the_text_cap(integration_client, image_file):
    p = image_file(".png", _PNG + bytes(1_100_000))
    r = integration_client.get("/api/files/revision" + _AUTH + f"&path={p}")
    assert r.status_code == 200, r.text
    assert r.json()["exists"] is True


def _revision(client, path):
    return client.get("/api/files/revision" + _AUTH + f"&path={path}")


def test_revision_probe_names_the_media_type_and_derives_an_image_revision_from_stat(
    integration_client, image_file,
):
    from doceditor.document_scope import revision

    data = _PNG + bytes(2_500_000)
    p = image_file(".png", data)
    st = p.stat()
    body = _revision(integration_client, p).json()
    assert body["media_type"] == "image/png"
    assert body["revision"] == f"stat-{st.st_size}-{st.st_mtime_ns}"
    assert body["revision"] != revision(data)
    assert _revision(integration_client, image_file(".svg", _SVG)).json()["media_type"] == "image/svg+xml"


def test_image_revision_changes_with_size_or_modification_time(integration_client, image_file):
    p = image_file(".png", _PNG)
    first = _revision(integration_client, p).json()["revision"]
    assert _revision(integration_client, p).json()["revision"] == first

    st = p.stat()
    os.utime(p, ns=(st.st_atime_ns, st.st_mtime_ns + 5_000_000_000))
    touched = _revision(integration_client, p).json()["revision"]
    assert touched != first

    p.write_bytes(_PNG + b"\x00")
    os.utime(p, ns=(st.st_atime_ns, st.st_mtime_ns + 5_000_000_000))
    assert _revision(integration_client, p).json()["revision"] not in (first, touched)


def test_revision_probe_has_no_media_type_for_a_document(integration_client, integration_workspace):
    p = integration_workspace / f"note-{uuid.uuid4().hex[:8]}.md"
    p.write_text("# n\n")
    try:
        body = _revision(integration_client, p).json()
    finally:
        p.unlink(missing_ok=True)
    assert body["exists"] is True and body["media_type"] is None


def test_revision_probe_never_reads_an_image_file(integration_client, image_file, monkeypatch):
    from pathlib import Path

    p = image_file(".png", _PNG + bytes(100_000))
    opened = []
    real_open = Path.open

    def watch(self, *args, **kwargs):
        opened.append(self)
        return real_open(self, *args, **kwargs)

    def refuse(self):
        raise AssertionError("the probe must not read image bytes")

    monkeypatch.setattr(Path, "read_bytes", refuse)
    monkeypatch.setattr(Path, "open", watch)
    assert _revision(integration_client, p).status_code == 200
    assert p not in opened


def test_text_revision_is_the_sha256_of_its_bytes(integration_client, integration_workspace):
    from doceditor.document_scope import revision

    p = integration_workspace / f"hash-{uuid.uuid4().hex[:8]}.md"
    p.write_bytes(b"# hashed\n")
    try:
        body = _revision(integration_client, p).json()
    finally:
        p.unlink(missing_ok=True)
    assert body["revision"] == revision(b"# hashed\n")


def test_the_raw_route_streams_the_file_and_the_ceiling_is_fifty_mebibytes(integration_client, image_file):
    from fastapi.responses import FileResponse

    from api import files
    from infra import config

    assert config.MAX_IMAGE_UPLOAD_BYTES == 50 * 1024 * 1024
    p = image_file(".png", _PNG)
    original = files.FileResponse
    made = []

    def spy(*args, **kwargs):
        made.append(args[0])
        return FileResponse(*args, **kwargs)

    files.FileResponse = spy
    try:
        assert _raw(integration_client, p).status_code == 200
    finally:
        files.FileResponse = original
    assert made == [p]  # a path handed to FileResponse streams in chunks; bytes are never held


@pytest.mark.parametrize("target", ["missing", "outside", "directory"])
def test_raw_and_text_reads_refuse_a_bad_path_the_same_way(
    integration_client, integration_workspace, tmp_path, target,
):
    if target == "missing":
        bad, status = integration_workspace / "nope.png", 404
    elif target == "outside":
        bad, status = tmp_path / "out.png", 400
        bad.write_bytes(_PNG)
    else:
        bad, status = integration_workspace, 400
    raw = _raw(integration_client, bad)
    text = integration_client.get("/api/files/content" + _AUTH + f"&path={bad}")
    assert (raw.status_code, text.status_code) == (status, status)
    assert raw.json() == text.json()


def test_the_image_limit_is_the_upload_limit_and_each_message_names_its_own_limit(
    integration_client, integration_workspace, image_file, monkeypatch,
):
    from infra import config
    from registry import file_types

    assert not hasattr(file_types, "MAX_IMAGE_BYTES")  # one limit, owned by config
    monkeypatch.setattr(config, "MAX_IMAGE_UPLOAD_BYTES", 100)
    p = image_file(".png", _PNG + bytes(200))
    for response in (_revision(integration_client, p), _raw(integration_client, p)):
        assert response.status_code == 413
        assert response.json()["detail"] == "Image too large to display"

    text = integration_workspace / f"long-{uuid.uuid4().hex[:8]}.md"
    text.write_bytes(b"x" * 1_000_001)
    try:
        refused = _revision(integration_client, text)
    finally:
        text.unlink(missing_ok=True)
    assert refused.status_code == 413
    assert refused.json()["detail"] == "File too large for the doc editor"


@pytest.mark.parametrize("suffix", [".png", ".svg"])
def test_opening_an_image_as_text_is_refused_with_a_typed_code(integration_client, image_file, suffix):
    p = image_file(suffix, _SVG if suffix == ".svg" else _PNG)
    r = integration_client.get("/api/files/content" + _AUTH + f"&path={p}")
    assert r.status_code == 415
    assert r.json()["detail"]["code"] == "image_file"
