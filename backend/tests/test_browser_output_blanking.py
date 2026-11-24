"""C6: a saved password is blanked from every kind of browser output an agent gets, in each of its encodings.

Each test saves a real login, then hands `_run_owned` (the one path a command's text takes back to an
agent) agent-browser output carrying the saved value: raw, URL-encoded or JSON-encoded, inside each kind of
output. The value must not come back in any of its forms.
"""
from __future__ import annotations

import html
import json
import os
import uuid
from contextlib import contextmanager
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from threading import Thread
from urllib.parse import quote, quote_plus

import pytest

import infra.saved_logins as saved_logins
from host_access import browser_cli, browser_output_blanking as blanking
from host_access.browser_session_state import BrowserSessionError
from host_access.browser_sessions import BrowserSessionOwner
from tools import browser_runtime


@pytest.fixture
def state(isolated_db, tmp_path, monkeypatch):
    root = tmp_path / "state"
    root.mkdir()
    monkeypatch.setattr("infra.config.STATE_ROOT", root)
    monkeypatch.setattr(browser_runtime, "_check_installed", lambda: None)
    return root


def _saved(state) -> tuple[str, str]:
    """Save a login whose value has a space, quotes, a slash, URL delimiters and Unicode; return it and its marker."""
    marker = uuid.uuid4().hex
    secret_text = f'Zq9 "sentinel"/ü&=+-{marker}'
    saved_logins.save_login(site="https://example.com", username="me@example.com", password=secret_text, saved_by="person")
    return secret_text, marker


# Under saved_logins.BLANKING_MIN characters, so it is never blanked.
SHORT_VALUE = "short7x"


def _chrome_text(value: str) -> str:
    """How Chrome 152 serialises a text node (checked on a real page): &, <, > and U+00A0 escaped; quotes kept."""
    return value.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;").replace("\u00a0", "&nbsp;")


def _chrome_attribute(value: str) -> str:
    """How Chrome 152 serialises an attribute value: the text escapes plus \" as &quot;; ' kept."""
    return _chrome_text(value).replace('"', "&quot;")

ENCODINGS = {
    "raw": lambda value: value,
    "url": lambda value: quote(value, safe=""),
    "url_form": lambda value: quote_plus(value, safe="-_.*"),
    "url_component": lambda value: quote(value, safe="-_.!~*'()"),
    "json": lambda value: json.dumps(value)[1:-1],
    "json_unicode": lambda value: json.dumps(value, ensure_ascii=False)[1:-1],
    "html_text": _chrome_text,
    "html_attribute": _chrome_attribute,
    "html_attribute_in_json": lambda value: json.dumps(_chrome_attribute(value))[1:-1],
}

# Each kind of output as agent-browser's --json writes it, with the page's text in it.
KINDS = {
    "snapshot": lambda text: (0, {"success": True, "data": {"snapshot": f'- heading "Welcome"\n- text: {text}'}}),
    "page_text": lambda text: (0, {"success": True, "data": {"text": f"Signed in. {text}"}}),
    "get": lambda text: (0, {"success": True, "data": {"value": text}}),
    "get_html": lambda text: (0, {"success": True, "data": {"html": f'<p>{text}</p><input value="{text}">'}}),
    "eval": lambda text: (0, {"success": True, "data": {"result": {"copied": text, "list": [text]}}}),
    "eval_string": lambda text: (0, {"success": True, "data": {"result": f"copied {text}"}}),
    "console": lambda text: (0, {"success": True, "data": {"messages": [{"type": "log", "text": text}]}}),
    "network": lambda text: (0, {"success": True, "data": {"requests": [
        {"url": f"https://example.com/login?p={text}", "postData": f"p={text}"}]}}),
    "error": lambda text: (1, {"success": False, "error": f"Element not found: {text}"}),
}


def _fake_owner(monkeypatch, outcome) -> None:
    async def run_browser(*_args, **_kwargs):
        if isinstance(outcome, BaseException):
            raise outcome
        returncode, payload = outcome
        result = browser_cli.CommandResult(returncode, json.dumps(payload), "")
        return browser_cli.render_model_result(result, redactions={})

    monkeypatch.setattr(browser_runtime.PROCESS_OWNER, "run_browser", run_browser)


def _absent(text: str, value: str, marker: str) -> None:
    for form in blanking.encodings(value) | {value, marker}:
        assert form not in text, form


@pytest.mark.asyncio
@pytest.mark.parametrize("kind", sorted(KINDS))
@pytest.mark.parametrize("encoding", sorted(ENCODINGS))
async def test_each_kind_of_output_comes_back_with_the_saved_value_blanked(state, monkeypatch, kind, encoding):
    secret_text, marker = _saved(state)
    _fake_owner(monkeypatch, KINDS[kind](ENCODINGS[encoding](secret_text)))
    text = await browser_runtime._run(["snapshot"], thread_id="thread-1")
    _absent(text, secret_text, marker)
    assert blanking.BLANK in text


@pytest.mark.asyncio
@pytest.mark.parametrize("encoding", sorted(ENCODINGS))
async def test_error_text_raised_by_the_browser_owner_is_blanked(state, monkeypatch, encoding):
    secret_text, marker = _saved(state)
    _fake_owner(monkeypatch, BrowserSessionError(f"browser said {ENCODINGS[encoding](secret_text)}"))
    with pytest.raises(browser_runtime.BrowserOwnerError) as raised:
        await browser_runtime._run_owned(["get", "text", "@e1"], thread_id="thread-1")
    _absent(raised.value.text, secret_text, marker)
    assert raised.value.text.startswith("Error: browser said [hidden]")


@pytest.mark.asyncio
async def test_the_saved_values_are_read_once_per_command(state, monkeypatch):
    _saved(state)
    reads = []
    real = blanking._saved_values
    monkeypatch.setattr(blanking, "_saved_values", lambda: reads.append(1) or real())
    _fake_owner(monkeypatch, KINDS["snapshot"]("\n".join(["line"] * 50)))
    await browser_runtime._run(["snapshot"], thread_id="thread-1")
    assert reads == [1]


@pytest.mark.asyncio
async def test_a_result_that_cant_be_checked_is_withheld(state, monkeypatch):
    secret_text, marker = _saved(state)

    def unreadable():
        raise OSError("values file unreadable")

    monkeypatch.setattr(blanking, "_saved_values", unreadable)
    _fake_owner(monkeypatch, KINDS["snapshot"](secret_text))
    assert await browser_runtime._run(["snapshot"], thread_id="thread-1") == blanking.UNCHECKED


@pytest.mark.asyncio
async def test_a_short_saved_value_and_unrelated_text_are_left_alone(state, monkeypatch):
    saved_logins.save_login(site="https://example.com", username="me", password=SHORT_VALUE, saved_by="person")
    _fake_owner(monkeypatch, KINDS["snapshot"]("short7x and ordinary text"))
    text = await browser_runtime._run(["snapshot"], thread_id="thread-1")
    assert "short7x and ordinary text" in text
    assert blanking.BLANK not in text


@contextmanager
def _page_showing(text: str, *, in_attributes: bool = False):
    fields = (f'<input id="field" value="{html.escape(text)}" data-copy="{html.escape(text)}">'
              if in_attributes else "")
    body = (f"<!doctype html><title>Account</title><main><h1>Account</h1><p>{html.escape(text)}</p>{fields}</main>"
            .encode())

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):  # noqa: N802 - BaseHTTPRequestHandler protocol name
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, *_args):
            pass

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{server.server_port}/account.html"
    finally:
        server.shutdown()
        thread.join(timeout=5)
        server.server_close()


@pytest.mark.integration
@pytest.mark.asyncio
async def test_a_saved_value_shown_on_a_real_page_comes_back_blanked_in_a_snapshot(state, monkeypatch):
    """C6 on real Chrome: the page shows the saved value as text; the agent's snapshot and page text have it blanked."""
    if os.environ.get("CORSIA_BROWSER_E2E") != "1":
        pytest.skip("set CORSIA_BROWSER_E2E=1 in the isolated host browser environment")
    secret_text, marker = _saved(state)
    thread_id = f"blanking-{uuid.uuid4().hex}"
    owner = BrowserSessionOwner(namespace="corsia-blanking", instance_key=uuid.uuid4().hex)

    async def run_browser(thread, args, **kwargs):
        kwargs.pop("lease_id", None)
        return await owner.run(thread, args, **kwargs)

    monkeypatch.setattr(browser_runtime.PROCESS_OWNER, "run_browser", run_browser)
    try:
        with _page_showing(secret_text) as url:
            assert not (await browser_runtime._run(["open", url], thread_id=thread_id)).startswith("Error:")
            outputs = [await browser_runtime._run(["snapshot"], thread_id=thread_id),
                       await browser_runtime._run(["get", "text", "main"], thread_id=thread_id)]
    finally:
        await owner.close_thread(thread_id)
    for text in outputs:
        _absent(text, secret_text, marker)
        assert blanking.BLANK in text, text


def _markup_value() -> str:
    return f"Ab1&\"<>' {uuid.uuid4().hex}"


# Every way a browser writes a value into markup: Chrome 152's text and attribute serialisation (checked on a real
# page), XMLSerializer's (U+00A0 kept as it is), and attributes as written before Chrome escaped < and > in them.
MARKUP_FORMS = {
    "text": _chrome_text,
    "attribute": _chrome_attribute,
    "xml_text": lambda value: value.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;"),
    "xml_attribute": lambda value: (value.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
                                   .replace('"', "&quot;")),
    "attribute_before_lt_gt": lambda value: (value.replace("&", "&amp;").replace('"', "&quot;")
                                            .replace(" ", "&nbsp;")),
}


@pytest.mark.parametrize("in_json", [False, True])
@pytest.mark.parametrize("form", sorted(MARKUP_FORMS))
def test_each_markup_form_of_a_value_is_blanked(form, in_json):
    value = _markup_value()
    written = MARKUP_FORMS[form](value)
    assert written != value
    if in_json:
        written = json.dumps(written)[1:-1]
    assert blanking.blank(f"<p>{written}</p>", [value]) == f"<p>{blanking.BLANK}</p>"


@pytest.mark.integration
@pytest.mark.asyncio
async def test_a_saved_value_in_a_real_pages_markup_comes_back_blanked_from_every_markup_command(state, monkeypatch):
    """Real Chrome: the saved value carries &, \", <, > and ', in text and in attributes; get html, an eval of
    outerHTML, an attribute read, page text and a snapshot each come back with it blanked."""
    if os.environ.get("CORSIA_BROWSER_E2E") != "1":
        pytest.skip("set CORSIA_BROWSER_E2E=1 in the isolated host browser environment")
    marker = uuid.uuid4().hex
    markup_text = f"Ab1&\"<>'{marker}"
    saved_logins.save_login(site="https://example.com", username="me@example.com", password=markup_text,
                            saved_by="person")
    thread_id = f"blanking-{uuid.uuid4().hex}"
    owner = BrowserSessionOwner(namespace="corsia-blanking", instance_key=uuid.uuid4().hex)

    async def run_browser(thread, args, **kwargs):
        kwargs.pop("lease_id", None)
        return await owner.run(thread, args, **kwargs)

    monkeypatch.setattr(browser_runtime.PROCESS_OWNER, "run_browser", run_browser)
    commands = (["get", "html", "main"], ["eval", "document.querySelector('main').outerHTML"],
                ["get", "attr", "#field", "data-copy"], ["get", "text", "main"], ["snapshot"])
    try:
        with _page_showing(markup_text, in_attributes=True) as url:
            assert not (await browser_runtime._run(["open", url], thread_id=thread_id)).startswith("Error:")
            outputs = {" ".join(args[:2]): await browser_runtime._run(args, thread_id=thread_id) for args in commands}
    finally:
        await owner.close_thread(thread_id)
    for command, text in outputs.items():
        assert not text.startswith("Error:"), (command, text)
        _absent(text, markup_text, marker)
        assert blanking.BLANK in text, command
    # get html returns the markup inside JSON, with the attribute's " written as &quot;.
    assert blanking.BLANK in outputs["get html"] and "&amp;" not in outputs["get html"]


def test_every_written_form_of_a_value_is_blanked_longest_first():
    value = "abc/def ghi\"jkl"
    text = " | ".join(sorted(blanking.encodings(value)))
    assert set(blanking.blank(text, [value]).split(" | ")) == {blanking.BLANK}
