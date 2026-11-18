"""Managed oMLX's native listener setting survives supervisor launches."""
import asyncio
import json
from types import SimpleNamespace

import pytest


@pytest.mark.parametrize(("saved", "target"), [
    ("0.0.0.0", "127.0.0.1"),
    ("::", "::1"),
    ("::1, 127.0.0.1", "::1"),
    ("192.0.2.5", "192.0.2.5"),
    ("127.0.0.1", "127.0.0.1"),
])
def test_managed_launch_preserves_saved_host_and_registers_client(tmp_path, monkeypatch, saved, target):
    from llm import managed_omlx_binary
    from llm.providers import omlx_supervisor as sup

    paths = SimpleNamespace(settings_path=tmp_path / "settings.json", base_path=tmp_path, model_dir=tmp_path / "models")
    paths.settings_path.write_text(json.dumps({"server": {"host": saved}}))
    monkeypatch.setattr(managed_omlx_binary, "managed_omlx_paths", lambda: paths)
    monkeypatch.setattr(sup, "RUNTIME_DIR", tmp_path)
    monkeypatch.setattr(sup, "_is_managed_omlx_server", lambda _: True)
    monkeypatch.setattr(sup, "_resolve_settings", lambda _: {})
    monkeypatch.setattr(sup, "_port_owner_pid", lambda *_: None)
    monkeypatch.setattr(sup.process, "managed_omlx_install_error", lambda: None)
    monkeypatch.setattr(sup.process, "managed_omlx_executable", lambda: "/fake/omlx")
    seen = {}

    def spawn(argv, _path):
        seen["argv"] = argv
        return SimpleNamespace(pid=12345)

    async def healthy(host, port, **_kwargs):
        seen["health"] = (host, port)
        return "healthy"

    monkeypatch.setattr(sup, "_spawn_process", spawn)
    monkeypatch.setattr(sup, "_wait_for_healthy", healthy)
    monkeypatch.setattr(sup, "_mark_endpoint_alive", lambda sid, host, port, _managed: seen.update(endpoint=(sid, host, port)))
    monkeypatch.setattr(sup, "watch", lambda *_: None)
    result = asyncio.run(sup.start_server({"server_id": "managed", "host": "127.0.0.1", "port": 9876}))
    assert result["ok"]
    argv = seen["argv"]
    assert argv[argv.index("--host") + 1] == saved
    assert seen["health"] == (target, 9876)
    assert seen["endpoint"] == ("managed", target, 9876)


@pytest.mark.parametrize("contents", [None, "{", "[]", '{"server": []}', '{"server": {"host": 12}}'])
def test_missing_or_malformed_settings_default_to_private(tmp_path, monkeypatch, contents):
    from llm import managed_omlx_binary
    from llm.providers.omlx_supervisor_config import managed_bind_host

    path = tmp_path / "settings.json"
    if contents is not None:
        path.write_text(contents)
    monkeypatch.setattr(managed_omlx_binary, "managed_omlx_paths", lambda: SimpleNamespace(settings_path=path))
    assert managed_bind_host() == "127.0.0.1"


def test_recovery_and_return_to_private_use_latest_saved_host(tmp_path, monkeypatch):
    from llm import managed_omlx_binary
    from llm.providers import omlx_supervisor as sup

    paths = SimpleNamespace(settings_path=tmp_path / "settings.json", base_path=tmp_path, model_dir=tmp_path / "models")
    monkeypatch.setattr(managed_omlx_binary, "managed_omlx_paths", lambda: paths)
    monkeypatch.setattr(sup, "RUNTIME_DIR", tmp_path)
    monkeypatch.setattr(sup, "_is_managed_omlx_server", lambda _: True)
    monkeypatch.setattr(sup, "_resolve_settings", lambda _: {})
    monkeypatch.setattr(sup, "_port_owner_pid", lambda *_: None)
    monkeypatch.setattr(sup, "_read_pid", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(sup.process, "managed_omlx_install_error", lambda: None)
    monkeypatch.setattr(sup.process, "managed_omlx_executable", lambda: "/fake/omlx")
    hosts = []
    monkeypatch.setattr(sup, "_spawn_process", lambda argv, _: hosts.append(argv[argv.index("--host") + 1]) or SimpleNamespace(pid=12345))

    async def healthy(*_args, **_kwargs):
        return "healthy"

    monkeypatch.setattr(sup, "_wait_for_healthy", healthy)
    monkeypatch.setattr(sup, "_mark_endpoint_alive", lambda *_: None)
    monkeypatch.setattr(sup, "watch", lambda *_: None)
    cfg = {"server_id": "managed", "host": "127.0.0.1", "port": 9876}
    for host in ("0.0.0.0", "127.0.0.1"):
        paths.settings_path.write_text(json.dumps({"server": {"host": host}}))
        assert asyncio.run(sup._start_recovery(cfg))["ok"]
    assert hosts == ["0.0.0.0", "127.0.0.1"]


def test_restart_selects_confirmed_endpoint_without_removing_other_servers(tmp_path, monkeypatch):
    from llm import managed_omlx_binary, network_discovery, network_endpoints
    from llm.providers import omlx_supervisor as sup

    sid = network_discovery.upsert_server("omlx", "managed-host-test", "Managed", "127.0.0.1", 9876, "online")
    other = network_discovery.upsert_server("external", "other-host-test", "Other", "127.0.0.1", 9877, "online", kind="external")
    paths = SimpleNamespace(settings_path=tmp_path / "settings.json", base_path=tmp_path, model_dir=tmp_path / "models")
    monkeypatch.setattr(managed_omlx_binary, "managed_omlx_paths", lambda: paths)
    monkeypatch.setattr(sup, "RUNTIME_DIR", tmp_path)
    monkeypatch.setattr(sup, "_is_managed_omlx_server", lambda _: True)
    monkeypatch.setattr(sup, "_resolve_settings", lambda _: {})
    monkeypatch.setattr(sup, "_port_owner_pid", lambda *_: None)
    monkeypatch.setattr(sup, "_read_pid", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(sup.process, "managed_omlx_install_error", lambda: None)
    monkeypatch.setattr(sup.process, "managed_omlx_executable", lambda: "/fake/omlx")
    monkeypatch.setattr(sup, "_spawn_process", lambda *_: SimpleNamespace(pid=12345))
    monkeypatch.setattr(sup, "watch", lambda *_: None)

    async def healthy(*_args, **_kwargs):
        return "healthy"

    monkeypatch.setattr(sup, "_wait_for_healthy", healthy)
    for host in ("192.168.1.12", "127.0.0.1"):
        paths.settings_path.write_text(json.dumps({"server": {"host": host}}))
        assert asyncio.run(sup._start_recovery({"server_id": sid, "host": "127.0.0.1", "port": 9876}))["ok"]
        assert network_endpoints.get_active_endpoint(sid)["host"] == host
    assert {ep["host"] for ep in network_endpoints.get_endpoints(sid)} == {"127.0.0.1", "192.168.1.12"}
    assert network_endpoints.get_active_endpoint(other)["host"] == "127.0.0.1"


def test_stop_checks_the_confirmed_managed_endpoint(tmp_path, monkeypatch):
    from llm.providers import omlx_supervisor as sup

    seen = []
    monkeypatch.setattr(sup, "RUNTIME_DIR", tmp_path)
    monkeypatch.setattr(sup, "_is_managed_omlx_server", lambda _: True)
    monkeypatch.setattr(sup, "_resolve_endpoint", lambda _: ("192.168.1.12", 9876))
    monkeypatch.setattr(sup, "_read_pid", lambda _sid, **kw: seen.append(kw) or None)
    asyncio.run(sup._stop_server({"server_id": "managed"}))
    assert seen == [{"host": "192.168.1.12", "port": 9876}]


def test_ipv6_urls_used_by_managed_consumers(monkeypatch):
    import httpx
    from llm import network_endpoints
    from llm.providers import omlx_endpoints

    cfg = {"host": "::1", "port": 9876}
    monkeypatch.setattr(network_endpoints, "get_active_endpoint", lambda _: {"host": "::1"})
    monkeypatch.setattr(network_endpoints, "_server_port", lambda _: 9876)
    urls = [network_endpoints.lan_base("::1", 9876), network_endpoints.mgmt_status_url("::1", 9876),
            network_endpoints.build_mgmt_base("server"), omlx_endpoints._api_base(cfg),
            omlx_endpoints._admin_base(cfg), omlx_endpoints.health_url("::1", 9876)]
    for url in urls:
        parsed = httpx.URL(url)
        assert parsed.host == "::1"
        assert parsed.port == 9876


@pytest.mark.parametrize("preload", [True, False])
def test_ipv6_readiness_uses_valid_http_authority(monkeypatch, preload):
    import httpx
    from llm.providers import omlx_supervisor as sup

    requests = []
    def respond(request):
        requests.append(request.url)
        return httpx.Response(200, json={"status": "ok", "data": []})

    real_client = httpx.AsyncClient
    monkeypatch.setattr(sup.httpx, "AsyncClient", lambda **kwargs: real_client(transport=httpx.MockTransport(respond), **kwargs))
    result = asyncio.run(sup._wait_for_healthy("::1", 9876, timeout_s=1, require_preload=preload))
    assert result == "healthy"
    assert requests[0].host == "::1"
    assert requests[0].port == 9876


@pytest.mark.parametrize("success", [True, False])
def test_probe_started_before_replacement_cannot_overwrite_new_state(monkeypatch, success):
    from llm import network_discovery, network_endpoints
    from llm.providers import omlx_public_probe, omlx_supervisor_endpoints

    sid = network_discovery.upsert_server("omlx", "host-probe-race", "Managed", "127.0.0.1", 9876, "online")
    entered, release = asyncio.Event(), asyncio.Event()

    async def probe(_client, _sid, host, _port):
        entered.set()
        await release.wait()
        return (host, 1, {"loaded_models": []}) if success else None

    async def observe(*_args, **_kwargs):
        pass

    monkeypatch.setattr(omlx_public_probe, "probe_registered_status", probe)
    monkeypatch.setattr(network_discovery, "record_reachable", observe)


    async def race():
        sweep = asyncio.create_task(network_discovery._sweep_managed(None, {"id": sid, "port": 9876}))
        await entered.wait()
        omlx_supervisor_endpoints.mark_endpoint_alive(sid, "192.168.1.12", 9876, True)
        release.set()
        await sweep

    asyncio.run(race())
    assert network_endpoints.get_active_endpoint(sid)["host"] == "192.168.1.12"
    old = next(ep for ep in network_endpoints.get_endpoints(sid) if ep["host"] == "127.0.0.1")
    assert old["last_ok_at"] is None
    assert network_discovery.get_server(sid)["status"] == "online"


def test_delayed_success_status_write_cannot_overwrite_replacement(monkeypatch):
    from llm import network_discovery, server_observation
    from llm.providers import omlx_supervisor_endpoints

    sid = network_discovery.upsert_server("omlx", "host-status-race", "Managed", "127.0.0.1", 9876, "online")
    generation = server_observation.observation_generation(sid)
    entered, release = asyncio.Event(), asyncio.Event()
    original = server_observation.run_in_db

    async def delayed(fn, *args, **kwargs):
        if fn is network_discovery.update_server_status:
            entered.set()
            await release.wait()
        return await original(fn, *args, **kwargs)

    monkeypatch.setattr(server_observation, "run_in_db", delayed)

    async def race():
        pending = asyncio.create_task(server_observation.record_reachable(
            {"id": sid}, loaded=None, rediscover=False, generation=generation, memory_used_mb=999,
        ))
        await entered.wait()
        omlx_supervisor_endpoints.mark_endpoint_alive(sid, "192.168.1.12", 9876, True)
        release.set()
        await pending

    asyncio.run(race())
    assert network_discovery.get_server(sid)["memory_used_mb"] != 999
