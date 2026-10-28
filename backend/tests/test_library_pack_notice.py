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
    row = {"external_id": external_id, "path": str(manifest)}
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
    monkeypatch.setattr(library_package, "load_index", lambda: SimpleNamespace(skills=(item,), generations={"demo": 1}))
    assert produce_once() == 1
    assert manifest.read_text() == "original"
