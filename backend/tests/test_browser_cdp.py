import pytest

from host_access import browser_cdp
from host_access.browser_cli import CommandResult


@pytest.mark.asyncio
async def test_endpoint_resolves_agent_browsers_browser_url_to_the_page(monkeypatch):
    monkeypatch.setattr(browser_cdp, "_page_endpoint", lambda _url: "ws://page.test/devtools/page/1")

    async def run(_args, _timeout):
        return CommandResult(0, '{"data":{"cdpUrl":"ws://browser.test/devtools/browser/1"}}', "")

    assert await browser_cdp._endpoint(run) == "ws://page.test/devtools/page/1"


@pytest.mark.asyncio
async def test_box_accepts_agent_browser_031s_direct_data_shape():
    async def run(_args, _timeout):
        return CommandResult(0, '{"data":{"x":1,"y":2,"width":3,"height":4}}', "")

    assert await browser_cdp._box(run, "@e2") == {"x": 1, "y": 2, "width": 3, "height": 4}
