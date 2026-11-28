"""Site clearing preserves unrelated cookies and refuses incomplete clears."""
import pytest

from host_access import browser_site_clear


class FakeCdp:
    def __init__(self, *_args, **_kwargs):
        self.cookies = [
            {"name": "sid", "value": "one", "domain": ".example.com", "path": "/"},
            {"name": "sid", "value": "two", "domain": ".other.com", "path": "/"},
        ]
        self.calls = []
        self.sessions = []
        self.reappear = False
        self.fail_method = None
        self.close_success = True
        self.workers = {"https://login.example.com", "https://other.org"}

    async def __aenter__(self):
        return self

    async def __aexit__(self, *_args):
        return None

    async def call_in(self, session, method, **params):
        self.calls.append((method, params))
        self.sessions.append((method, session))
        if method == self.fail_method:
            raise ConnectionError("connection lost")
        if method == "Storage.getCookies":
            return {"cookies": list(self.cookies)}
        if method == "Target.getTargets":
            return {"targetInfos": [{"type": "page", "targetId": "p", "url": "https://app.example.com:443/"}]}
        if method == "Target.attachToTarget":
            return {"sessionId": params["targetId"]}
        if method == "Page.getFrameTree":
            return {"frameTree": {"frame": {"url": "https://app.example.com:443/"}}}
        if method == "Target.closeTarget":
            return {"success": self.close_success}
        if method == "Target.createTarget":
            return {"targetId": "blank"}
        if method == "Storage.clearDataForOrigin" and params.get("storageTypes") == "all":
            self.workers.discard(params.get("origin"))
        if method == "Network.deleteCookies":
            self.cookies = [row for row in self.cookies if row["domain"] != params["domain"]]
            if self.reappear:
                self.cookies.append({"name": "sid", "value": "again", "domain": ".example.com", "path": "/"})
        return {}


@pytest.mark.asyncio
async def test_clear_site_and_keep_unrelated_cookie(monkeypatch):
    fake = FakeCdp()
    monkeypatch.setattr(browser_site_clear.browser_cdp, "CdpClient", lambda *_a, **_kw: fake)
    await browser_site_clear.clear("ws://test", "https://login.example.com")
    assert [row["domain"] for row in fake.cookies] == [".other.com"]
    assert fake.workers == {"https://other.org"}
    calls = [(method, args.get("origin")) for method, args in fake.calls if method == "Storage.clearDataForOrigin"]
    assert ("Storage.clearDataForOrigin", "https://app.example.com:443") in calls
    assert ("Storage.clearDataForOrigin", "https://login.example.com") in calls
    assert all(args.get("storageTypes") == "all" for method, args in fake.calls if method == "Storage.clearDataForOrigin")
    assert fake.calls.index(("Target.createTarget", {"url": "about:blank"})) < fake.calls.index(
        ("Target.closeTarget", {"targetId": "p"}))
    assert ("Target.closeTarget", {"targetId": "blank"}) in fake.calls
    assert ("Network.enable", "blank") in fake.sessions
    assert all(session == "blank" for method, session in fake.sessions
               if method in {"Storage.clearDataForOrigin", "Network.deleteCookies"})


@pytest.mark.asyncio
async def test_recorded_closed_origin_is_cleared(monkeypatch):
    fake = FakeCdp()
    monkeypatch.setattr(browser_site_clear.browser_cdp, "CdpClient", lambda *_a, **_kw: fake)
    await browser_site_clear.clear("ws://test", "https://example.com",
                                   recorded_origins=("https://closed.example.com:8443",))
    assert ("Storage.clearDataForOrigin", {"origin": "https://closed.example.com:8443",
                                          "storageTypes": "all"}) in fake.calls


def test_inherited_frame_security_origin_is_included():
    tree = {"frame": {"url": "about:blank", "securityOrigin": "https://sub.example.com:8443"}}
    assert browser_site_clear._frames(tree, "example.com") == {"https://sub.example.com:8443"}


@pytest.mark.asyncio
async def test_target_close_must_be_confirmed(monkeypatch):
    fake = FakeCdp()
    fake.close_success = False
    monkeypatch.setattr(browser_site_clear.browser_cdp, "CdpClient", lambda *_a, **_kw: fake)
    with pytest.raises(RuntimeError, match="target close"):
        await browser_site_clear.clear("ws://test", "https://example.com")
    assert ("Target.closeTarget", {"targetId": "blank"}) in fake.calls


@pytest.mark.asyncio
async def test_cookie_return_after_clear_is_failure(monkeypatch):
    fake = FakeCdp()
    fake.reappear = True
    monkeypatch.setattr(browser_site_clear.browser_cdp, "CdpClient", lambda *_a, **_kw: fake)
    with pytest.raises(RuntimeError, match="remain"):
        await browser_site_clear.clear("ws://test", "https://example.com")


@pytest.mark.asyncio
async def test_lost_connection_mid_clear_fails(monkeypatch):
    fake = FakeCdp()
    fake.fail_method = "Storage.clearDataForOrigin"
    monkeypatch.setattr(browser_site_clear.browser_cdp, "CdpClient", lambda *_a, **_kw: fake)
    with pytest.raises(ConnectionError):
        await browser_site_clear.clear("ws://test", "https://example.com")
