"""Explicit native-browser job: real Chrome refuses every channel to the serving CORSIA, not to another instance."""
from __future__ import annotations

import asyncio
import ipaddress
import json
import os
from pathlib import Path
from urllib.parse import quote

import pytest

from host_access import browser_cdp, browser_own_address, browser_ports
from host_access.browser_chrome import ChromeProcess
from tests.browser_own_address_fixture import SENTINEL, ephemeral_pair, servers

pytestmark = [pytest.mark.integration, pytest.mark.skipif(
    os.environ.get("CORSIA_NATIVE_BROWSER_TEST") != "1", reason="explicit native-browser job only")]

UNRESOLVED = "net::ERR_NAME_NOT_RESOLVED"
WAIT = "window.__wait=window.__wait||(ms=>new Promise(d=>{const on=e=>{if(e.data==='reached'){d('reached')}};" \
       "addEventListener('message',on);setTimeout(()=>d('refused'),ms)}));true"
_FRAME = "(()=>{{const w=__wait(2500);{body};return w}})()"


def _q(value: str) -> str:
    return json.dumps(value)


def _worker(url: str, code: str) -> str:
    source = _q(code.replace("URL", _q(url)))
    return ("new Promise(d=>{const w=new Worker(URL.createObjectURL(new Blob([" + source +
            "])));w.onmessage=e=>d(e.data)})")


def _in_frame(attach: str) -> str:
    """Wait for the page a hidden frame loads to report back; ``attach`` loads it into ``f``."""
    return _FRAME.format(body="const f=document.createElement('iframe');f.name='f'+Math.random();"
                              "document.body.append(f);" + attach)


def channels(base: str, outside: str) -> dict[str, str]:
    """JavaScript for each way a page reaches ``base``; every one yields 'reached' or 'refused'."""
    ws = base.replace("http", "ws", 1) + "/ws"
    fetched = "fetch(URL).then(r=>r.text()).then(()=>postMessage('reached'),()=>postMessage('refused'))"
    socket_ = "const s=new WebSocket('" + ws + "');s.onopen=()=>postMessage('reached');s.onerror=()=>postMessage('refused')"
    link = f"const a=document.createElement('a');a.href={_q(base + '/frame')};a.target=f.name;document.body.append(a);a.click()"
    form = f"const m=document.createElement('form');m.method='POST';m.action={_q(base + '/frame')};m.target=f.name;" \
           "document.body.append(m);m.submit()"
    redirect = quote(base + "/frame")
    return {
        "link click": _in_frame(link),
        "form post": _in_frame(form),
        "iframe": _in_frame(f"f.src={_q(base + '/frame')}"),
        "redirect from an outside origin (iframe)": _in_frame(f"f.src={_q(f'{outside}/redirect?target={redirect}')}"),
        "popup": _FRAME.format(body=f"window.open({_q(base + '/frame')})"),
        "fetch": f"fetch({_q(base + '/x')}).then(r=>r.text()).then(t=>t.includes('{SENTINEL}')?'reached':'refused')"
                 ".catch(()=>'refused')",
        "XHR": f"new Promise(d=>{{const x=new XMLHttpRequest();x.open('GET',{_q(base + '/x')});"
               "x.onload=()=>d('reached');x.onerror=()=>d('refused');x.send()})",
        "WebSocket": f"new Promise(d=>{{const s=new WebSocket({_q(ws)});s.onopen=()=>{{s.close();d('reached')}};"
                     "s.onerror=()=>d('refused')})",
        "image": f"new Promise(d=>{{const i=new Image();i.onload=()=>d('reached');i.onerror=()=>d('refused');"
                 f"i.src={_q(base + '/i.gif')}}})",
        "script": f"new Promise(d=>{{const s=document.createElement('script');s.onload=()=>d('reached');"
                  f"s.onerror=()=>d('refused');s.src={_q(base + '/s.js')};document.head.append(s)}})",
        "worker fetch": _worker(base + "/x", fetched),
        "worker WebSocket": _worker(base, socket_),
        "shared worker fetch": "new Promise(d=>{const w=new SharedWorker(URL.createObjectURL(new Blob([" +
            _q("onconnect=e=>{const p=e.ports[0];" + fetched.replace("URL", _q(base + "/x")).replace(
                "postMessage", "p.postMessage") + "}") + "])));w.port.onmessage=e=>d(e.data);w.port.start()})",
        "service worker fetch": "navigator.serviceWorker.register('/sw.js').then(()=>navigator.serviceWorker.ready)"
            ".then(r=>new Promise(d=>{navigator.serviceWorker.onmessage=e=>d(e.data.startsWith('read:')?'reached':"
            f"'refused');r.active.postMessage({_q(base + '/x')})}}))",
    }


def _is_address(name: str) -> bool:
    try:
        ipaddress.ip_address(name.strip("[]"))
    except ValueError:
        return False
    return True


async def _evaluate(cdp, expression):
    result = await cdp.call("Runtime.evaluate", expression=expression, awaitPromise=True, returnByValue=True)
    assert "exceptionDetails" not in result, (expression, result["exceptionDetails"])
    return result["result"].get("value")


async def _close_extra_pages(browser, keep):
    for item in (await browser.call("Target.getTargets"))["targetInfos"]:
        if item["type"] == "page" and item["targetId"] != keep:
            await browser.call("Target.closeTarget", targetId=item["targetId"])


@pytest.fixture
async def session(tmp_path, monkeypatch):
    """One real Chrome launched with the production argv for a serving instance on a free port."""
    executable = Path(os.environ["CORSIA_NATIVE_CHROME_BINARY"]).resolve(strict=True)
    async with servers() as fixture:
        monkeypatch.setattr(browser_ports, "reserve_pair", ephemeral_pair)
        monkeypatch.setattr(browser_own_address, "_backend_ports", lambda: {fixture.serving})
        monkeypatch.setattr(browser_own_address, "_frontend_ports", lambda ports: frozenset())
        chrome = await ChromeProcess.launch(str(executable), profile=str(tmp_path / "profile"), headless=True,
                                            ua=None, record=(tmp_path, "session123456"))
        try:
            page_endpoint = await asyncio.to_thread(browser_cdp._page_endpoint, chrome.endpoint)
            async with browser_cdp.CdpClient(chrome.endpoint) as browser, browser_cdp.CdpClient(page_endpoint) as page:
                await page.call("Page.navigate", url=f"http://127.0.0.1:{fixture.other}/page")
                await asyncio.sleep(.5)
                await _evaluate(page, WAIT)
                yield fixture, browser, page, page_endpoint
        finally:
            await chrome.close()


async def test_every_page_channel_refuses_the_serving_instance(session):
    """Each channel is refused toward the serving port under two names and works toward the other instance."""
    fixture, browser, page, page_endpoint = session
    outside = f"http://localhost:{fixture.other}"
    table = {}
    for name in ("127.0.0.1", "localhost"):
        serving, control = f"http://{name}:{fixture.serving}", f"http://{name}:{fixture.other}"
        attempts, controls = channels(serving, outside), channels(control, outside)
        for channel in attempts:
            refused = await _evaluate(page, attempts[channel])
            reached = await _evaluate(page, controls[channel])
            table[f"{channel} @ {name}"] = {"serving": refused, "other instance": reached}
            await _close_extra_pages(browser, page_endpoint.rsplit("/", 1)[1])
            assert (refused, reached) == ("refused", "reached"), (channel, name, refused, reached)
    assert not fixture.requests["serving"], fixture.requests["serving"]
    assert fixture.requests["other"], "the other instance never received a request"
    print(json.dumps(table, indent=1), flush=True)


async def test_popup_page_and_agent_script_refuse_the_serving_instance(session):
    """A page the first page opened, and an agent script run in it, cannot reach the serving instance either."""
    fixture, browser, page, page_endpoint = session
    before = {t["targetId"] for t in (await browser.call("Target.getTargets"))["targetInfos"]}
    await _evaluate(page, f"window.open('http://127.0.0.1:{fixture.other}/popup'); true")
    created = []
    for _ in range(60):
        created = [t for t in (await browser.call("Target.getTargets"))["targetInfos"]
                   if t["type"] == "page" and t["targetId"] not in before]
        if created:
            break
        await asyncio.sleep(.05)
    assert created, "window.open did not create a page target"
    popup_endpoint = page_endpoint.rsplit("/", 1)[0] + "/" + created[0]["targetId"]
    results = {}
    async with browser_cdp.CdpClient(popup_endpoint) as popup:
        await asyncio.sleep(.5)
        for label, port in (("serving", fixture.serving), ("other", fixture.other)):
            base = f"http://127.0.0.1:{port}"
            results[label] = [await _evaluate(popup, channels(base, "")[kind]) for kind in ("fetch", "WebSocket")]
    assert results == {"serving": ["refused", "refused"], "other": ["reached", "reached"]}, results
    assert not fixture.requests["serving"]


async def test_navigation_refused_under_every_name_for_this_machine(session):
    """Navigation to the serving port fails to resolve under every name; other ports load, but not on a Tailscale name."""
    fixture, browser, page, page_endpoint = session
    names = ["127.0.0.1", "localhost", "LOCALHOST", "a.b.localhost", "[::1]", "0.0.0.0", "[::ffff:7f00:1]"]
    names += sorted(browser_own_address._machine_hosts() - set(names))
    tailnet = browser_own_address.tailnet_hosts()
    names += sorted(tailnet - set(names))
    outcomes = {}
    for name in names:
        refused = await page.call("Page.navigate", url=f"http://{name}:{fixture.serving}/typed")
        assert refused.get("errorText") == UNRESOLVED, (name, refused)
        target = quote(f"http://{name}:{fixture.serving}/redirected", safe="")
        redirected = await page.call("Page.navigate", url=f"http://127.0.0.1:{fixture.other}/redirect?target={target}")
        assert redirected.get("errorText") == UNRESOLVED, (name, redirected)
        control = await page.call("Page.navigate", url=f"http://{name}:{fixture.other}/control")
        outcomes[name] = {"serving": refused["errorText"], "serving redirect": redirected["errorText"],
                          "other instance": control.get("errorText", "loaded")}
        if name in tailnet:  # Tailscale serve fronts CORSIA on 443, so the whole name is closed, any port
            assert control.get("errorText") == UNRESOLVED, (name, control)
        elif name in ("127.0.0.1", "localhost", "LOCALHOST", "a.b.localhost"):
            assert "errorText" not in control, (name, control)
        elif _is_address(name):  # Chrome here cannot resolve this machine's .local name even on an open port
            assert control.get("errorText") != UNRESOLVED, (name, control)
    assert not fixture.requests["serving"]
    print(json.dumps(outcomes, indent=1), flush=True)
