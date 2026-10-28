import hashlib

from library.pack_digest import pack_digest


def fixture_pack():
    files = {"manifest.yaml": b"schema: corsia.pack/1\nkind: blueprint\nname: demo\nversion: 1.0.0\n", "CHANGELOG.md": b"first\n"}
    item = {"kind": "pack", "pack_kind": "blueprint", "name": "demo", "version": "1.0.0", "withdrawn": False,
            "source": {"repository": "example/library", "path": "packs/demo", "commit": "a" * 40},
            "files": [{"path": p, "sha256": hashlib.sha256(data).hexdigest()} for p, data in files.items()],
            "digest": pack_digest(files)}
    return item, files


def setup(monkeypatch, tmp_path, files):
    from core import skill_resolution
    from library import items, pack_fetch
    from registry import models, registration_writer

    monkeypatch.setattr(pack_fetch, "get_scope_coordinator", lambda scope, db: "owner" if scope == "project" else None)
    monkeypatch.setattr(skill_resolution, "skill_scope_root", lambda **kwargs: tmp_path / "thread")
    monkeypatch.setattr(items, "_fetch_raw", lambda url: files[url.split("/packs/demo/", 1)[1]])
    registrations = []
    monkeypatch.setattr(pack_fetch, "_registered", lambda *args: bool(registrations))
    monkeypatch.setattr(registration_writer, "register_artifact", lambda **kwargs: registrations.append(kwargs) or {"id": "artifact"})
    monkeypatch.setattr(models, "update_surfacing", lambda *args, **kwargs: None)
    return registrations


def test_registration_completeness_uses_real_owner_and_surfacing(isolated_db, tmp_path):
    from infra.database import get_db
    from library.pack_fetch import _registered
    from registry import models
    from tests.event_routing_helpers import mk_session

    owner = mk_session("pack-registration-owner")
    db = get_db()
    try:
        folder = tmp_path / "demo"
        assert not _registered(owner, "demo", "1.0.0", folder, db)
        artifact = models.register_artifact(integration="library", type="installed-pack",
            external_id=f"{owner}/demo/1.0.0", parent_thread_id=owner, path=str(folder / "manifest.yaml"), db=db)
        assert not _registered(owner, "demo", "1.0.0", folder, db)
        models.update_surfacing(artifact["id"], surfacing="quiet", parent_thread_id=owner, actor_thread_id=owner, db=db)
        assert _registered(owner, "demo", "1.0.0", folder, db)
        assert not _registered(owner, "demo", "2.0.0", folder, db)
        assert not _registered(owner, "demo", "1.0.0", tmp_path / "other", db)
        assert not _registered("other-owner", "demo", "1.0.0", folder, db)
        from library.pack_fetch import _PACK_REGISTRATION_SQL
        plan = db.execute("EXPLAIN QUERY PLAN " + _PACK_REGISTRATION_SQL,
                          ("library", "installed-pack", f"{owner}/demo/1.0.0",
                           str(folder / "manifest.yaml"), owner, "quiet")).fetchall()
        assert any("SEARCH identities" in row["detail"] and "external_id=?" in row["detail"] for row in plan)
    finally:
        db.close()


def test_can_fetch_uses_designated_scope_coordinator(monkeypatch):
    from library import pack_fetch

    calls = []
    monkeypatch.setattr(pack_fetch, "get_scope_coordinator", lambda scope, db: calls.append(scope) or {"workspace": "workspace-owner", "project": "project-owner"}.get(scope))
    db = object()
    assert pack_fetch.can_fetch("workspace-owner", None, db)
    assert pack_fetch.can_fetch("project-owner", "project", db)
    assert not pack_fetch.can_fetch("workspace-owner", "project", db)
    assert not pack_fetch.can_fetch("worker", "missing", db)
    assert calls == ["workspace", "project", "project", "missing"]


def test_tool_returns_fetch_owner_authorization(monkeypatch):
    from types import SimpleNamespace
    from library import pack_fetch
    from tools import skill_library

    monkeypatch.setattr(pack_fetch, "can_fetch", lambda *args: False)
    monkeypatch.setattr(skill_library, "_item", lambda *args: (SimpleNamespace(index_age="just now"), fixture_pack()[0]))
    assert skill_library._fetch_pack("demo", "worker", "project", object())["error"] == "not_conductor"


def test_fetch_and_retry_never_overwrite(monkeypatch, tmp_path):
    from library.pack_fetch import fetch_pack

    item, files = fixture_pack()
    registrations = setup(monkeypatch, tmp_path, files)
    result = fetch_pack(item, "owner", "project", tmp_path, object())
    assert result["outcome"] == "fetched"
    folder = tmp_path / "thread" / "packs" / "demo" / "1.0.0"
    assert (folder / "manifest.yaml").read_bytes() == files["manifest.yaml"]
    assert registrations[0]["parent_thread_id"] == "owner"
    retry = fetch_pack(item, "owner", "project", tmp_path, object())
    assert retry["outcome"] == "already_fetched" and retry["path"] == str(folder)
    assert len(registrations) == 1
    (folder / "manifest.yaml").write_text("changed")
    mismatch = fetch_pack(item, "owner", "project", tmp_path, object())
    assert mismatch["error"] == "copy_differs" and mismatch["path"] == str(folder)
    assert "does not match the published bytes" in mismatch["message"]
    assert "Remove that folder" in mismatch["message"]
    assert (folder / "manifest.yaml").read_text() == "changed"


def test_registration_failure_is_recoverable_without_overwrite(monkeypatch, tmp_path):
    from library.pack_fetch import fetch_pack
    from registry import registration_writer

    item, files = fixture_pack()
    registrations = setup(monkeypatch, tmp_path, files)
    def fail_registration(**kwargs):
        raise ValueError("registration failed")
    monkeypatch.setattr(registration_writer, "register_artifact", fail_registration)
    first = fetch_pack(item, "owner", "project", tmp_path, object())
    assert first["error"] == "registration_failed"
    assert "registration failed" not in first["message"]
    folder = tmp_path / "thread" / "packs" / "demo" / "1.0.0"
    before = (folder / "manifest.yaml").stat().st_ino
    monkeypatch.setattr(registration_writer, "register_artifact", lambda **kwargs: registrations.append(kwargs) or {"id": "artifact"})
    assert fetch_pack(item, "owner", "project", tmp_path, object())["outcome"] == "fetched"
    assert (folder / "manifest.yaml").stat().st_ino == before
    assert len(registrations) == 1


def test_refuses_unauthorized_connector_and_byte_mismatch(monkeypatch, tmp_path):
    from library.pack_fetch import fetch_pack

    item, files = fixture_pack()
    setup(monkeypatch, tmp_path, files)
    assert fetch_pack(item, "owner", "different-project", tmp_path, object())["error"] == "not_conductor"
    item["pack_kind"] = "connector"
    assert fetch_pack(item, "owner", "project", tmp_path, object())["error"] == "not_blueprint"
    item["pack_kind"] = "blueprint"
    files["CHANGELOG.md"] = b"tampered"
    assert fetch_pack(item, "owner", "project", tmp_path, object())["error"] == "integrity"
    assert not (tmp_path / "thread").exists()


def test_pack_state_reports_update_only_when_every_held_version_is_older():
    from library.pack_fetch import pack_state

    item = {"name": "demo", "version": "1.1.0"}
    assert pack_state(item, {"demo": {"1.0.0"}}) == "update_available"
    assert pack_state(item, {"demo": {"1.0.0", "2.0.0"}}) == "not_fetched"
    assert pack_state(item, {"demo": {"1.1.0", "1.0.0"}}) == "fetched"
    assert pack_state(item, {}) == "not_fetched"
