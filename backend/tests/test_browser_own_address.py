"""The serving CORSIA's own addresses: which names and ports the agents' browser refuses."""
from __future__ import annotations

import asyncio
import socket
import subprocess
import sys

import pytest

from core.devices_runtime import runtime
from host_access import browser_chrome, browser_launch, browser_navigation, browser_own_address as own
from host_access.browser_identity import BrowserIdentityError
from host_access.browser_session_state import BrowserSessionError
from infra.devices_listener import DEFAULT_PORT, FALLBACK_PORTS

SERVING, FRONTEND = 4242, 5173
MACHINE = {"localhost", "127.0.0.1", "0.0.0.0", "[::1]", "studio", "studio.local", "192.168.1.20"}
TAILNET = {"100.64.1.2"}
OUTSIDE = ("mac.tail1234.ts.net", 443)


@pytest.fixture
def serving(monkeypatch):
    """An instance listening on SERVING with a frontend on FRONTEND, on a machine with a Tailscale name."""
    monkeypatch.setattr(own, "_page_port", lambda: SERVING)
    monkeypatch.setattr(own, "_backend_ports", lambda: {SERVING})
    monkeypatch.setattr(own, "_frontend_ports", lambda ports: frozenset({FRONTEND}))
    monkeypatch.setattr(own, "tailnet_addresses", lambda: set(TAILNET))
    monkeypatch.setattr(own, "outside_endpoint", lambda: OUTSIDE)
    monkeypatch.setattr(own, "_machine_hosts", lambda: set(MACHINE))


@pytest.mark.parametrize("url", [
    f"http://127.0.0.1:{SERVING}/", f"http://localhost:{SERVING}/x?y=1", f"http://LOCALHOST:{SERVING}/",
    f"http://localhost.:{SERVING}/", f"http://a.b.Localhost:{SERVING}/", f"http://[::1]:{SERVING}/",
    f"http://0.0.0.0:{SERVING}/", f"http://studio.local:{SERVING}/", f"http://STUDIO:{SERVING}/",
    f"http://192.168.1.20:{SERVING}/", f"https://127.0.0.1:{SERVING}/", f"ws://localhost:{SERVING}/socket",
    f"http://127.0.0.1:{FRONTEND}/", f"http://localhost:{DEFAULT_PORT}/", f"http://localhost:{FALLBACK_PORTS[-1]}/",
    "https://mac.tail1234.ts.net/", "https://mac.tail1234.ts.net:443/", "wss://Mac.Tail1234.ts.net./",
    "http://100.64.1.2:9999/", f"https://example.com:{SERVING}/",  # any name at the serving port: it may resolve here
])
def test_the_serving_instance_is_its_own_address(serving, url):
    assert own.is_own_address(url), url


@pytest.mark.parametrize("url", [
    f"http://127.0.0.1:{SERVING + 1}/", "http://localhost:3110/", f"http://[::1]:{SERVING - 1}/",
    f"http://a.localhost:{SERVING + 1}/", "https://example.com/", "http://studio.local:80/", "http://studio/",
    "https://192.168.1.20/", "http://100.64.1.3:9999/", "about:blank", "http://[bad",
    "http://mac.tail1234.ts.net/", "https://mac.tail1234.ts.net:8443/",  # the outside name at another port
])
def test_another_instance_and_the_web_stay_reachable(serving, url):
    assert not own.is_own_address(url), url


def test_well_known_ports_are_matched_by_machine_name_only(monkeypatch, serving):
    monkeypatch.setattr(own, "_backend_ports", lambda: {443})
    assert own.is_own_address("https://localhost/")
    assert own.is_own_address("https://[::1]/")
    assert own.is_own_address("https://studio.local:443/")
    assert not own.is_own_address("https://example.com/")
    assert own.is_own_address("https://a.localhost/")
    rules = own.resolver_rules().split(", ")
    assert {"MAP localhost:443 ~NOTFOUND", "MAP *.localhost:443 ~NOTFOUND"} <= set(rules)
    assert not any(rule.startswith("MAP *:443") for rule in rules)


def test_resolver_rules_name_each_port_and_each_tailscale_name(serving):
    rules = own.resolver_rules().split(", ")
    for port in (SERVING, FRONTEND, DEFAULT_PORT, *FALLBACK_PORTS):
        assert f"MAP *:{port} ~NOTFOUND" in rules
    assert {"MAP mac.tail1234.ts.net:443 ~NOTFOUND", "MAP 100.64.1.2 ~NOTFOUND"} <= set(rules)
    assert "MAP mac.tail1234.ts.net ~NOTFOUND" not in rules
    assert all(not rule.startswith(("MAP *:80 ", "MAP *:443 ")) for rule in rules)


def test_this_backends_own_ports_are_found_and_an_in_process_page_server_is_not(monkeypatch):
    held = socket.socket()
    held.bind(("127.0.0.1", 0))
    held.listen()
    monkeypatch.setenv("BACKEND_PORT", "4999")
    monkeypatch.setenv("CORSIA_OTLP_PORT", "4998")
    try:
        ports = own._backend_ports()
        assert {4999, 4998} <= ports and held.getsockname()[1] not in ports
    finally:
        held.close()


def test_without_a_configured_port_the_default_port_is_served(monkeypatch):
    from infra.default_port import DEFAULT_BACKEND_PORT
    monkeypatch.delenv("BACKEND_PORT", raising=False)
    assert DEFAULT_BACKEND_PORT in own._backend_ports()


def _frontend(backend: int) -> subprocess.Popen:
    code = ("import socket,sys,time\ns=socket.socket();s.bind(('127.0.0.1',0));s.listen()\n"
            "print(s.getsockname()[1],flush=True);time.sleep(60)")
    return subprocess.Popen([sys.executable, "-c", code], stdout=subprocess.PIPE, text=True,
                            env={"CORSIA_BACKEND_URL": f"http://127.0.0.1:{backend}"})


def test_a_frontend_in_front_of_this_backend_is_found_and_another_is_not(monkeypatch):
    monkeypatch.setattr(own, "_frontend_scan", (float("-inf"), frozenset()))
    ours, theirs = _frontend(SERVING), _frontend(SERVING + 7)
    try:
        ours_port, theirs_port = (int(item.stdout.readline()) for item in (ours, theirs))
        found = own._frontend_ports({SERVING})
        assert ours_port in found and theirs_port not in found
    finally:
        ours.kill()
        theirs.kill()


@pytest.mark.parametrize(("saved", "endpoint"), [
    ("https://Mac.Tail1234.ts.net.", ("mac.tail1234.ts.net", 443)),
    ("home.example.com:8443", ("home.example.com", 8443)),
    (None, None),
])
def test_the_outside_address_is_refused_at_its_own_port_only(monkeypatch, saved, endpoint):
    monkeypatch.setattr(runtime, "outside_address", saved)
    assert own.outside_endpoint() == endpoint


def test_a_saved_domain_with_a_port_leaves_its_website_reachable(monkeypatch, serving):
    monkeypatch.setattr(own, "outside_endpoint", lambda: ("home.example.com", 8443))
    assert own.is_own_address("https://home.example.com:8443/")
    assert not own.is_own_address("https://home.example.com/")
    rules = own.resolver_rules().split(", ")
    assert "MAP home.example.com:8443 ~NOTFOUND" in rules and "MAP home.example.com ~NOTFOUND" not in rules
    assert "https://home.example.com:8443" in own._serving_origins()


def test_only_page_ports_are_cleared_not_the_measure_receivers(monkeypatch, serving):
    monkeypatch.setattr(own, "_backend_ports", lambda: {SERVING, 47921})
    origins = own._serving_origins()
    assert f"http://localhost:{SERVING}" in origins and "https://mac.tail1234.ts.net" in origins
    assert not any(origin.endswith(":47921") for origin in origins)


def test_clearing_stops_at_its_budget_when_the_browser_stops_answering(monkeypatch, serving):
    from host_access import browser_cdp

    class Silent:
        calls = 0

        def __init__(self, endpoint):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *exc):
            return False

        async def call(self, method, **params):
            Silent.calls += 1
            await asyncio.sleep(3600)

    async def first_page(endpoint):
        return "ws://127.0.0.1:1/page"

    monkeypatch.setattr(browser_cdp, "CdpClient", Silent)
    monkeypatch.setattr(own, "_first_page", first_page)
    monkeypatch.setattr(own, "_CLEAR_TIMEOUT", 0.05)
    monkeypatch.setattr(own, "_CLEAR_BUDGET", 0.2)
    loop = asyncio.new_event_loop()
    try:
        started = loop.time()
        loop.run_until_complete(own.clear_serving_storage("ws://127.0.0.1:1/browser"))
        assert loop.time() - started < 1.0
    finally:
        loop.close()
    assert 0 < Silent.calls < len(own._serving_origins())


def test_typing_the_serving_address_gives_the_agent_one_plain_line(serving):
    with pytest.raises(BrowserSessionError) as refused:
        browser_navigation.normalize_url(f"localhost:{SERVING}/settings")
    assert str(refused.value) == own.REFUSAL
    assert browser_navigation.normalize_url(f"localhost:{SERVING + 1}") == f"http://localhost:{SERVING + 1}"


def test_chrome_is_launched_with_the_rules_and_a_stray_override_is_refused(serving, monkeypatch, tmp_path):
    argv = browser_chrome.chrome_argv("chrome", 9222, tmp_path, headless=True, ua=None)
    assert f"--host-resolver-rules={own.resolver_rules()}" in argv
    monkeypatch.setenv("AGENT_BROWSER_ARGS", "--host-resolver-rules=MAP * 1.2.3.4")
    with pytest.raises(BrowserIdentityError):
        browser_launch.inherited_args()
