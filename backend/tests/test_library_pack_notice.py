from types import SimpleNamespace

from events.connectors import library
from events.platform import DIGESTED, deliver_platform_event_sync
from library.producer import _pack_notice_payload
from registry import models
from tests.event_routing_helpers import mk_session


def test_newer_pack_notice_routes_to_registered_owner(isolated_db, tmp_path):
    owner = mk_session("blueprint-notice-owner")
    manifest = tmp_path / "manifest.yaml"
    manifest.write_text("schema: corsia.pack/1\nkind: blueprint\nname: demo\nversion: 1.0.0\n")
    external_id = f"{owner}/demo/1.0.0"
    models.register_artifact(integration="library", type=library.INSTALLED_PACK_TYPE,
                             external_id=external_id, parent_thread_id=owner, path=str(manifest))
    item = {"kind": "pack", "pack_kind": "blueprint", "withdrawn": False,
            "name": "demo", "version": "1.1.0", "digest": "a" * 64}
    row = {"external_id": external_id, "path": str(manifest), "created_at": "2026-01-01T00:00:00Z"}
    before = manifest.read_bytes()
    payload = _pack_notice_payload(row, {"demo": item}, {"demo": 1})
    deliveries = deliver_platform_event_sync("library", payload)
    assert [(result.outcome, result.thread_id) for result in deliveries] == [(DIGESTED, owner)]
    assert manifest.read_bytes() == before
    item["version"] = "1.0.0"
    assert _pack_notice_payload(row, {"demo": item}, {"demo": 1}) is None


def test_existing_producer_finds_and_delivers_pack_update(isolated_db, tmp_path, monkeypatch):
    from infra import config
    from infra.user_profile import PROFILE_RELATIVE_PATH, USE_COMMUNITY_LABEL
    import library as library_package
    from library.producer import produce_once

    profile = tmp_path / PROFILE_RELATIVE_PATH
    profile.parent.mkdir(parents=True)
    profile.write_text(f"{USE_COMMUNITY_LABEL} Yes\n")
    monkeypatch.setattr(config, "WORKSPACE", tmp_path)
    owner = mk_session("pack-producer-owner")
    manifest = tmp_path / "manifest.yaml"
    manifest.write_text("original")
    models.register_artifact(integration="library", type=library.INSTALLED_PACK_TYPE,
                             external_id=f"{owner}/demo/1.0.0", parent_thread_id=owner, path=str(manifest))
    item = {"kind": "pack", "pack_kind": "blueprint", "withdrawn": False,
            "name": "demo", "version": "2.0.0", "digest": "a" * 64}
    monkeypatch.setattr(library_package, "load_index", lambda: SimpleNamespace(items=(item,), generations={"demo": 1}))
    assert produce_once() == 1
    assert manifest.read_text() == "original"


def test_newest_pack_only_and_withdrawal_when_community_off(isolated_db, tmp_path, monkeypatch):
    from infra import config
    from infra.user_profile import PROFILE_RELATIVE_PATH, USE_COMMUNITY_LABEL
    from infra.database import get_db
    import library as library_package
    from library.producer import produce_once

    profile = tmp_path / PROFILE_RELATIVE_PATH
    profile.parent.mkdir(parents=True)
    profile.write_text(f"{USE_COMMUNITY_LABEL} Yes\n")
    monkeypatch.setattr(config, "WORKSPACE", tmp_path)
    owner = mk_session("pack-multiple-owner")
    manifest = tmp_path / "manifest.yaml"
    manifest.write_text("original")
    for version in ("1.0.0", "2.0.0"):
        models.register_artifact(integration="library", type=library.INSTALLED_PACK_TYPE,
                                 external_id=f"{owner}/demo/{version}", parent_thread_id=owner, path=str(manifest))
    item = {"kind": "pack", "pack_kind": "blueprint", "withdrawn": False,
            "withdrawn_reason": None, "name": "demo", "version": "3.0.0", "digest": "a" * 64}
    monkeypatch.setattr(library_package, "load_index", lambda: SimpleNamespace(items=(item,), generations={"demo": 1}))
    assert produce_once() == 1
    assert produce_once() == 0
    db = get_db()
    try:
        notes = db.execute("SELECT summary FROM context_change_journal WHERE object_kind='thread_context' AND object_id=?", (owner,)).fetchall()
        assert len(notes) == 1 and "2.0.0" in notes[0]["summary"]
        installed = db.execute("SELECT r.created_at FROM artifact_registrations r JOIN artifact_identities i ON i.id=r.artifact_id WHERE i.external_id=?", (f"{owner}/demo/2.0.0",)).fetchone()[0]
    finally:
        db.close()
    payload = _pack_notice_payload({"external_id": f"{owner}/demo/2.0.0", "created_at": installed}, {"demo": item}, {"demo": 1})
    assert payload["installed_at"] == installed
    profile.write_text(f"{USE_COMMUNITY_LABEL} No\n")
    item.update(withdrawn=True, withdrawn_reason="unsafe")
    assert produce_once() == 1
    assert produce_once() == 0
    db = get_db()
    try:
        messages = db.execute("SELECT content FROM messages WHERE thread_id=? AND id LIKE 'platform-event:%'", (owner,)).fetchall()
        assert len(messages) == 1 and "library.pack_withdrawn" in messages[0]["content"]
    finally:
        db.close()
    monkeypatch.setattr(library_package, "load_index", lambda: SimpleNamespace(items=(), generations={}))
    assert produce_once() == 0
    assert manifest.read_text() == "original"
