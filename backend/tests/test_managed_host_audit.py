"""Host saves and confirmed endpoints survive lifecycle bookkeeping."""
import asyncio
import json
from types import SimpleNamespace

import pytest


@pytest.mark.parametrize("entry", ["adopt", "reconcile"])
def test_liveness_only_adoption_retains_other_confirmed_endpoints(monkeypatch, entry):
    from llm import network_discovery, network_endpoints
    from llm.providers import omlx_supervisor as sup

    sid = network_discovery.upsert_server("omlx", "host-adoption-audit", "Managed", "127.0.0.1", 9876, "online")
    network_discovery.upsert_endpoint(sid, "192.0.2.5", ok=True)
    monkeypatch.setattr(sup, "_is_managed_omlx_server", lambda _: True)
    monkeypatch.setattr(sup, "watch", lambda *_: None)
    if entry == "adopt":
        monkeypatch.setattr(sup, "_read_pid", lambda *_a, **_kw: 12345)
        monkeypatch.setattr(sup, "_process_alive", lambda _: True)
        monkeypatch.setattr(sup, "_port_owner_pid", lambda *_: 12345)
        assert asyncio.run(sup.start_server({"server_id": sid, "host": "127.0.0.1", "port": 9876}))["already_running"]
    else:
        monkeypatch.setattr(sup, "_resolve_endpoint_via_freshness", lambda _: ("127.0.0.1", 9876))
        monkeypatch.setattr(sup, "_read_pid", lambda *_a, **_kw: 12345)
        monkeypatch.setattr(sup, "_process_alive", lambda _: True)
        assert sup.reconcile({"server_id": sid})["adopted"]
    assert all(ep["last_ok_at"] is not None for ep in network_endpoints.get_endpoints(sid))


def test_native_settings_save_during_port_probe_is_preserved(tmp_path, monkeypatch):
    from llm import managed_omlx_binary as binary
    from llm.providers import omlx_process

    paths = SimpleNamespace(base_path=tmp_path, model_dir=tmp_path / "models", settings_path=tmp_path / "settings.json")
    paths.settings_path.write_text(json.dumps({"server": {"host": "127.0.0.1"}}))
    monkeypatch.setattr(binary, "managed_omlx_paths", lambda: paths)
    saved = {"server": {"host": "0.0.0.0"}, "cache": {"ssd_cache_dir": "/chosen/cache"}, "scheduler": {"max_num_seqs": 7}}

    def free(_port):
        paths.settings_path.write_text(json.dumps(saved))
        return True

    monkeypatch.setattr(omlx_process, "port_is_free", free)
    _, port = binary.reserve_managed_omlx_port()
    actual = json.loads(paths.settings_path.read_text())
    assert actual == {**saved, "server": {"host": "0.0.0.0", "port": port}, "model": {"model_dir": str(paths.model_dir)}}


def test_reserving_unchanged_managed_fields_does_not_rewrite_native_settings(tmp_path, monkeypatch):
    from pathlib import Path
    from llm import managed_omlx_binary as binary
    from llm.providers import omlx_process

    paths = SimpleNamespace(base_path=tmp_path, model_dir=tmp_path / "models", settings_path=tmp_path / "settings.json")
    raw = {"server": {"host": "0.0.0.0", "port": binary.MANAGED_PORTS[0]}, "model": {"model_dir": str(paths.model_dir)}}
    paths.settings_path.write_text(json.dumps(raw))
    monkeypatch.setattr(binary, "managed_omlx_paths", lambda: paths)
    monkeypatch.setattr(omlx_process, "port_owner_pid", lambda *_: None)
    original_write = Path.write_text

    def reject_settings_write(path, *args, **kwargs):
        assert path != paths.settings_path, "unchanged native settings must not be rewritten"
        return original_write(path, *args, **kwargs)

    monkeypatch.setattr(Path, "write_text", reject_settings_write)
    assert binary.reserve_managed_omlx_port()[1] == binary.MANAGED_PORTS[0]
