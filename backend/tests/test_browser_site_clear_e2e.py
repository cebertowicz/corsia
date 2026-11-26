"""Real Chrome proof that clearing a site removes its service worker."""
import os
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from threading import Thread
from uuid import uuid4

import pytest

from host_access import browser_cdp, browser_session_commands, browser_site_clear
from host_access.browser_sessions import BrowserSessionOwner


class Handler(BaseHTTPRequestHandler):
    def do_GET(self):
        body = b"self.addEventListener('fetch', () => {});" if self.path == "/sw.js" else b"<title>Site</title>"
        self.send_response(200)
        self.send_header("Content-Type", "application/javascript" if self.path == "/sw.js" else "text/html")
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *_args):
        pass


@pytest.mark.integration
@pytest.mark.asyncio
async def test_site_clear_removes_service_worker():
    if os.environ.get("CORSIA_BROWSER_E2E") != "1":
        pytest.skip("requires isolated host browser environment")
    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = Thread(target=server.serve_forever, daemon=True)
    thread.start()
    site = f"http://127.0.0.1:{server.server_port}"
    name = f"site-clear-{uuid4().hex}"
    owner = BrowserSessionOwner(namespace="corsia-site-clear", instance_key=uuid4().hex)
    try:
        await owner.run(name, ("open", site), session="new")
        endpoint = await owner.page_endpoint(name)
        async with browser_cdp.CdpClient(endpoint) as page:
            result = await page.call("Runtime.evaluate", expression="(async () => { await navigator.serviceWorker.register('/sw.js'); await navigator.serviceWorker.ready; return (await navigator.serviceWorker.getRegistrations()).length; })()", awaitPromise=True, returnByValue=True)
            assert result["result"]["value"] == 1
        browser_endpoint = await browser_session_commands.browser_endpoint(owner, name)
        async with browser_cdp.CdpClient(browser_endpoint) as browser:
            await browser.call("Target.createTarget", url="about:blank")
        await browser_site_clear.clear(browser_endpoint, site)
        await owner.run(name, ("open", site), session="existing")
        endpoint = await owner.page_endpoint(name)
        async with browser_cdp.CdpClient(endpoint) as page:
            result = await page.call("Runtime.evaluate", expression="navigator.serviceWorker.getRegistrations().then(rows => rows.length)", awaitPromise=True, returnByValue=True)
            assert result["result"]["value"] == 0
    finally:
        await owner.close_thread(name)
        server.shutdown()
        server.server_close()
        thread.join()
