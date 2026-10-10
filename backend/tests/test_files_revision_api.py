"""Read-only content-revision endpoint used by open artifact tabs."""

from __future__ import annotations

import os
import sys
import uuid

import pytest
from auth_fixtures import OWNER_TOKEN

sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))

pytestmark = pytest.mark.integration

_TOKEN = OWNER_TOKEN
_AUTH = "?token=" + _TOKEN


@pytest.fixture
def rev_file(integration_workspace):
    p = integration_workspace / f"revision-{uuid.uuid4().hex[:8]}.md"
    p.write_text("# One\n")
    yield p
    p.unlink(missing_ok=True)


def _revision(client, path, artifact_id=None):
    url = "/api/files/revision" + _AUTH + f"&path={path}"
    if artifact_id:
        url += f"&artifact_id={artifact_id}"
    r = client.get(url)
    assert r.status_code == 200, r.text
    return r.json()


def test_revision_tracks_content_and_ignores_unrelated_registration(
    integration_client, rev_file
):
    """The revision moves on a content write, not on registration."""
    before = _revision(integration_client, str(rev_file))
    assert before["exists"] is True
    assert before["writable"] is True
    assert before["revision"]
    assert before["artifact_id"] is None

    registered = integration_client.post(
        "/api/files/register" + _AUTH,
        json={"path": str(rev_file)},
    )
    assert registered.status_code == 200
    artifact_id = registered.json()["artifact"]["id"]

    after_register = _revision(integration_client, str(rev_file))
    assert after_register["revision"] == before["revision"]
    assert after_register["artifact_id"] == artifact_id

    written = integration_client.put(
        "/api/files/content" + _AUTH,
        json={"path": str(rev_file), "text": "# One\n\n# Two\n"},
    )
    assert written.status_code == 200, written.text
    assert written.json()["revision"]

    after_write = _revision(integration_client, str(rev_file))
    assert after_write["revision"] != before["revision"]
    assert after_write["revision"] == written.json()["revision"]


def test_revision_is_read_only_and_does_not_register_the_read(
    integration_client, integration_workspace
):
    """Polling must not create the identity or traffic it is watching for."""
    p = integration_workspace / f"unregistered-{uuid.uuid4().hex[:8]}.md"
    p.write_text("# Untouched\n")
    try:
        seen = []
        from registry import service as registry_service

        original = registry_service.notify_changed
        registry_service.notify_changed = lambda payload: seen.append(payload)
        try:
            body = _revision(integration_client, str(p))
        finally:
            registry_service.notify_changed = original

        assert body["exists"] is True
        assert body["artifact_id"] is None
        assert seen == []
    finally:
        p.unlink(missing_ok=True)


def test_revision_reports_missing_file_without_erroring(
    integration_client, integration_workspace
):
    """A deleted tab target needs an explicit unavailable answer, not a 404."""
    missing = integration_workspace / "never-created.md"
    body = _revision(integration_client, str(missing))
    assert body == {
        "exists": False,
        "path": None,
        "revision": None,
        "media_type": None,
        "size": None,
        "writable": False,
        "artifact_id": None,
        "artifact": None,
        "freshness": None,
    }


def test_resolved_artifact_survives_relocation(integration_client, integration_workspace):
    """A moved document relocates by artifact id instead of going unavailable."""
    import shutil

    original = integration_workspace / f"moving-{uuid.uuid4().hex[:8]}.md"
    original.write_text("# Movable\n")
    registered = integration_client.post(
        "/api/files/register" + _AUTH,
        json={"path": str(original)},
    )
    assert registered.status_code == 200
    artifact_id = registered.json()["artifact"]["id"]

    moved_dir = integration_workspace / "relocated"
    moved_dir.mkdir(exist_ok=True)
    moved = moved_dir / original.name
    shutil.move(str(original), str(moved))

    from infra.database import get_db

    db = get_db()
    try:
        db.execute(
            "UPDATE artifact_identities SET path=? WHERE id=?",
            (str(moved.resolve()), artifact_id),
        )
        db.commit()
    finally:
        db.close()

    # Without the artifact id the old path is simply gone.
    assert _revision(integration_client, str(original))["exists"] is False

    # With it, the registry is the authority on where the document now lives.
    relocated = _revision(integration_client, str(original), artifact_id=artifact_id)
    assert relocated["exists"] is True
    assert relocated["path"] == str(moved.resolve())
    assert relocated["revision"]

    moved.unlink(missing_ok=True)


def test_revision_reports_system_context_file_as_readonly(
    integration_client, integration_workspace
):
    """System bootstrap files open readonly instead of failing at first save."""
    ctx_dir = integration_workspace / "context"
    ctx_dir.mkdir(exist_ok=True)
    p = ctx_dir / f"SYSTEM-{uuid.uuid4().hex[:8]}.md"
    p.write_text("# System bootloader\n")
    try:
        body = _revision(integration_client, str(p))
        assert body["exists"] is True
        assert body["writable"] is False

        content = integration_client.get(
            "/api/files/content" + _AUTH + f"&path={p}"
        )
        assert content.status_code == 200, content.text
        assert content.json()["writable"] is False
        assert content.json()["revision"] == body["revision"]

        refused = integration_client.put(
            "/api/files/content" + _AUTH,
            json={"path": str(p), "text": "# tampered\n"},
        )
        assert refused.status_code == 403
    finally:
        p.unlink(missing_ok=True)


def test_revision_rejects_paths_outside_scope_roots(integration_client, tmp_path):
    outside = tmp_path / "outside.md"
    outside.write_text("nope\n")
    r = integration_client.get("/api/files/revision" + _AUTH + f"&path={outside}")
    assert r.status_code == 400


def test_revision_is_bytes_based_even_when_mtime_and_size_are_preserved(
    integration_client, rev_file
):
    before = _revision(integration_client, str(rev_file))
    stat = rev_file.stat()
    rev_file.write_text("# Two\n")
    os.utime(rev_file, ns=(stat.st_atime_ns, stat.st_mtime_ns))

    checked = _revision(integration_client, str(rev_file))
    content = integration_client.get("/api/files/content" + _AUTH + f"&path={rev_file}")
    written = integration_client.put(
        "/api/files/content" + _AUTH,
        json={"path": str(rev_file), "text": "# Tri\n"},
    )

    assert checked["revision"] != before["revision"]
    assert checked["size"] == before["size"]
    assert content.status_code == 200
    assert content.json()["revision"] == checked["revision"]
    assert written.status_code == 200
    assert written.json()["revision"] == _revision(integration_client, str(rev_file))["revision"]


@pytest.mark.parametrize("retire", [True, False], ids=["archived", "unregistered"])
def test_revision_treats_nonactive_or_unregistered_identity_as_unavailable(
    integration_client, rev_file, retire
):
    registered = integration_client.post(
        "/api/files/register" + _AUTH,
        json={"path": str(rev_file)},
    )
    artifact_id = registered.json()["artifact"]["id"]

    from infra.database import get_db
    from registry import models as registry_models

    db = get_db()
    try:
        if retire:
            registry_models.retire(artifact_id, db=db)
        else:
            db.execute("DELETE FROM artifact_registrations WHERE artifact_id=?", (artifact_id,))
            db.commit()
    finally:
        db.close()

    assert rev_file.is_file()
    assert _revision(integration_client, str(rev_file), artifact_id) == {
        "exists": False,
        "path": None,
        "revision": None,
        "media_type": None,
        "size": None,
        "writable": False,
        "artifact_id": None,
        "artifact": None,
        "freshness": None,
    }


def test_revision_follows_active_identity_path_even_when_old_file_still_exists(
    integration_client, integration_workspace
):
    old = integration_workspace / f"old-{uuid.uuid4().hex[:8]}.md"
    moved = integration_workspace / f"moved-{uuid.uuid4().hex[:8]}.md"
    old.write_text("# Old\n")
    moved.write_text("# Current\n")
    registered = integration_client.post(
        "/api/files/register" + _AUTH,
        json={"path": str(old)},
    )
    artifact_id = registered.json()["artifact"]["id"]

    from infra.database import get_db

    db = get_db()
    try:
        db.execute(
            "UPDATE artifact_identities SET path=? WHERE id=?",
            (str(moved.resolve()), artifact_id),
        )
        db.commit()
    finally:
        db.close()
    try:
        resolved = _revision(integration_client, str(old), artifact_id)
        assert resolved["exists"] is True
        assert resolved["path"] == str(moved.resolve())
    finally:
        old.unlink(missing_ok=True)
        moved.unlink(missing_ok=True)


def test_revision_hides_foreign_artifact_identity_without_enumerating_it(integration_client, integration_workspace):
    from infra.database import get_db
    from registry import models as registry_models
    from tests.registry_helpers import make_session

    foreign_file = integration_workspace / f"foreign-{uuid.uuid4().hex[:8]}.md"
    foreign_file.write_text("# Private\n")
    db = get_db()
    try:
        foreign_thread = make_session(db, prefix="foreign", user_id="leksi")
        foreign = registry_models.register_artifact(
            integration="workspace",
            type="document",
            external_id=str(foreign_file.resolve()),
            path=str(foreign_file.resolve()),
            parent_thread_id=foreign_thread,
            db=db,
        )
    finally:
        db.close()
    try:
        r = integration_client.get(
            "/api/files/revision" + _AUTH + f"&path={foreign_file}&artifact_id={foreign['id']}"
        )
        assert r.status_code == 200
        assert r.json() == {
            "exists": False,
            "path": None,
            "revision": None,
            "media_type": None,
            "size": None,
            "writable": False,
            "artifact_id": None,
        "artifact": None,
        "freshness": None,
        }
    finally:
        foreign_file.unlink(missing_ok=True)
