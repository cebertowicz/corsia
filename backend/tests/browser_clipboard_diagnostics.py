"""Failure details for clipboard tests on their synthetic, owned browser pages."""
import json
import logging
import time

import pytest

from host_access import browser_clipboard as clipboard


class _ClipboardTrace(logging.Handler):
    def __init__(self):
        super().__init__()
        self.events = []
        self.endpoints = set()
        self.original_call = clipboard._call
        self.original_selection = clipboard.clipboard_selection
        self.original_paste = clipboard.clipboard_paste

    def emit(self, record):
        self.events.append({"cdp_error": record.getMessage()})

    async def call(self, cdp, method, step, **params):
        self.endpoints.add(cdp.endpoint)
        entry = {"method": method, "step": step, "endpoint": cdp.endpoint}
        self.events.append(entry)
        started = time.monotonic()
        try:
            result = await self.original_call(cdp, method, step, **params)
            if method == "Runtime.evaluate":
                entry["response"] = result
            return result
        except Exception as exc:
            entry["error"] = repr(exc)
            raise
        finally:
            entry["seconds"] = round(time.monotonic() - started, 6)

    async def action(self, name, operation, *args):
        try:
            return await operation(*args)
        except Exception as exc:
            self.events.append({"action": name, "error": repr(exc)})
            raise

    async def selection(self, endpoint, cut):
        return await self.action("copy/cut", self.original_selection, endpoint, cut)

    async def paste(self, endpoint, text):
        return await self.action("paste", self.original_paste, endpoint, text)

    async def report(self):
        pages = []
        for endpoint in sorted(self.endpoints):
            try:
                pages.append(await _page_state(endpoint))
            except Exception as exc:
                pages.append({"endpoint": endpoint, "diagnostic_error": repr(exc)})
        print("Native clipboard failure diagnostics:", json.dumps({
            "trace": self.events, "after_failure": pages,
        }, indent=2))


async def _page_state(endpoint):
    async with clipboard.CdpClient(endpoint) as cdp:
        context, url = await clipboard._world(cdp)
        isolated = await clipboard._evaluate(cdp, context, """({
            installed: window.__corsiaClipboardListener,
            sequence: window.__corsiaClipboardSequence,
            record: window.__corsiaClipboardRecord,
            focus: document.hasFocus(), visibility: document.visibilityState
        })""")
        page = await clipboard._call(cdp, "Runtime.evaluate", "diagnostic page state",
            expression="({focus:document.hasFocus(),visibility:document.visibilityState,events:window.clipboardEvents})",
            returnByValue=True)
        return {"endpoint": endpoint, "url": url, "isolated": isolated, "page": page}


@pytest.fixture
async def native_clipboard_diagnostics(native_owner, monkeypatch, request):
    """Expose hidden helper errors only when a synthetic clipboard test fails."""
    trace = _ClipboardTrace()
    failed_before = request.session.testsfailed
    logger = logging.getLogger("host_access.browser_cdp")
    previous_level = logger.level
    logger.addHandler(trace)
    logger.setLevel(logging.DEBUG)
    monkeypatch.setattr(clipboard, "_call", trace.call)
    monkeypatch.setattr(clipboard, "clipboard_selection", trace.selection)
    monkeypatch.setattr(clipboard, "clipboard_paste", trace.paste)
    try:
        yield
    finally:
        try:
            if request.session.testsfailed > failed_before:
                # native_owner still owns these fixture pages. This is state after
                # failure, not a claim about focus at the instant of key dispatch.
                await trace.report()
        finally:
            logger.removeHandler(trace)
            logger.setLevel(previous_level)
