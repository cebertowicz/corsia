"""A browser command that fails unexpectedly leaves a log record, and the record carries no page data."""
from __future__ import annotations

import logging
import os
import uuid
from contextlib import contextmanager
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from threading import Thread

import pytest

from host_access import browser_cdp, browser_control
from host_access.browser_session_state import BrowserSessionError
from host_access.browser_sessions import BrowserSessionOwner
from tools import browser_runtime
from tools.browser_runtime import COMMAND_FAILED, BrowserCloseTool

SECRET_URL = "https://secret.example.test/results?token=abc"


@pytest.fixture(autouse=True)
def installed(monkeypatch):
    monkeypatch.setattr(browser_runtime, "_check_installed", lambda: None)


def _records(caplog, logger_name):
    return [record for record in caplog.records if record.name == logger_name and record.levelno >= logging.WARNING]


async def test_unexpected_command_failure_logs_its_type_only_never_its_text_or_traceback(monkeypatch, caplog):
    sentinel = "typed-value-5c1e"

    async def fail(*_args, **_kwargs):
        # An exception's own text can quote page text or a typed value.
        raise RuntimeError(f"plan write failed near {sentinel}")

    monkeypatch.setattr(browser_runtime.PROCESS_OWNER, "run_browser", fail)
    with caplog.at_level(logging.DEBUG):
        result = await browser_runtime._run(["open", SECRET_URL], thread_id="thread-1")

    assert result == f"Error: {COMMAND_FAILED}"
    (record,) = _records(caplog, browser_runtime.__name__)
    assert record.exc_info is None and record.exc_text is None
    message = record.getMessage()
    assert "thread-1" in message and "open" in message and "RuntimeError" in message
    assert "secret.example.test" not in message and "token" not in message and SECRET_URL not in message
    assert sentinel not in caplog.text and all(sentinel not in repr(each.__dict__) for each in caplog.records)


async def test_close_failure_is_logged_with_traceback(monkeypatch, caplog):
    async def fail(_thread_id):
        raise RuntimeError("close failed")

    monkeypatch.setattr(browser_runtime.PROCESS_OWNER, "close_browser", fail)
    with caplog.at_level(logging.WARNING, logger=browser_runtime.__name__):
        result = await BrowserCloseTool().execute(_thread_id="thread-2")

    assert result.startswith("Error: browser cleanup could not be verified")
    (record,) = _records(caplog, browser_runtime.__name__)
    assert record.exc_info is not None and "thread-2" in record.getMessage()


# The served page holds no sentinel; its script copies the URL's fragment into two fields' value attributes, so the
# sentinel reaches the browser only as page state that the probe's CDP frames carry back. The password field is
# hidden, so the probe reads it but the page stays the agent's and the snapshot runs.
MIRROR_PAGE = b"""<!doctype html><title>Account</title><main><h1>Account</h1>
<input id="name"><input id="pass" type="password" autocomplete="current-password" hidden></main>
<script>for (const id of ["name", "pass"]) document.getElementById(id).setAttribute("value", location.hash.slice(1));</script>"""


@contextmanager
def _mirror_page():
    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):  # noqa: N802 - BaseHTTPRequestHandler protocol name
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(MIRROR_PAGE)))
            self.end_headers()
            self.wfile.write(MIRROR_PAGE)

        def log_message(self, *_args):
            pass

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    Thread(target=server.serve_forever, daemon=True).start()
    try:
        yield f"http://127.0.0.1:{server.server_port}/account.html"
    finally:
        server.shutdown()
        server.server_close()


@pytest.mark.integration
@pytest.mark.asyncio
async def test_page_state_in_cdp_frames_never_reaches_a_log_at_debug(isolated_db, tmp_path, monkeypatch, caplog):
    """Real Chrome at DEBUG: a page mirrors a sentinel into value attributes; the password probe's CDP frames carry
    those attributes, and neither the probe nor a snapshot puts the sentinel in any log record."""
    if os.environ.get("CORSIA_BROWSER_E2E") != "1":
        pytest.skip("set CORSIA_BROWSER_E2E=1 in the isolated host browser environment")
    (tmp_path / "state").mkdir()
    monkeypatch.setattr("infra.config.STATE_ROOT", tmp_path / "state")
    sentinel = f"mirrored{uuid.uuid4().hex}"
    thread_id = f"debug-log-{uuid.uuid4().hex}"
    owner = BrowserSessionOwner(namespace="corsia-debug-log", instance_key=uuid.uuid4().hex)
    probes = []
    real_probe = browser_cdp.page_password_probe

    async def run_browser(thread, args, **kwargs):
        kwargs.pop("lease_id", None)
        return await owner.run(thread, args, **kwargs)

    async def probe(endpoint, known):
        probes.append(await real_probe(endpoint, known))
        return probes[-1]

    monkeypatch.setattr(browser_runtime.PROCESS_OWNER, "run_browser", run_browser)
    monkeypatch.setattr(browser_cdp, "page_password_probe", probe)
    try:
        with _mirror_page() as url, caplog.at_level(logging.DEBUG):
            assert not (await browser_runtime._run(["open", f"{url}#{sentinel}"], thread_id=thread_id)).startswith("Error:")
            assert not (await browser_runtime._run(["snapshot"], thread_id=thread_id)).startswith("Error:")
    finally:
        await owner.close_thread(thread_id)

    # The probe read the page holding the mirrored password field, so its frames carried the sentinel.
    assert any(each.present_nodes for each in probes)
    assert caplog.records
    assert sentinel not in caplog.text and all(sentinel not in repr(each.__dict__) for each in caplog.records)


async def test_failed_page_probe_is_logged_before_it_is_mapped_to_page_check_failed(caplog):
    async def probe(_known):
        raise RuntimeError("page probe unavailable")

    with caplog.at_level(logging.WARNING, logger=browser_control.__name__):
        with pytest.raises(BrowserSessionError, match=browser_control.PAGE_CHECK_FAILED):
            await browser_control.require_page_access("thread-3", probe, ("snapshot",))

    (record,) = _records(caplog, browser_control.__name__)
    assert record.exc_info is not None and record.exc_info[0] is RuntimeError
    assert "thread-3" in record.getMessage()
