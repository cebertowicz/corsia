"""C6: a saved password is blanked from every kind of browser output an agent gets, in each of its encodings.

Each test saves a real login, then hands `_run_owned` (the one path a command's text takes back to an
agent) agent-browser output carrying the saved value: raw, URL-encoded or JSON-encoded, inside each kind of
output. The value must not come back in any of its forms.
"""
from __future__ import annotations

import asyncio
import html
import json
import os
import re
import time
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
async def test_a_short_saved_value_is_hidden_but_not_inside_a_word(state, monkeypatch):
    saved_logins.save_login(site="https://example.com", username="me", password=SHORT_VALUE, saved_by="person")
    _fake_owner(monkeypatch, KINDS["snapshot"]("short7x and xshort7xy"))
    text = await browser_runtime._run(["snapshot"], thread_id="thread-1")
    assert "[hidden] and xshort7xy" in text


@pytest.mark.parametrize("value", ["hunter22pass", "123-45-6789"])
@pytest.mark.parametrize("escape", [r"\n", r"\r", r"\t", r"\b", r"\f", r"\u00a0", r"\u00A0", "%20", "%0A", r"\\n"])
@pytest.mark.parametrize("side", ["before", "after"])
def test_escaped_boundaries_hide_values(value, escape, side):
    text = f"label{escape}{value}" if side == "before" else f"{value}{escape}label"
    expected = f"label{escape}[hidden]" if side == "before" else f"[hidden]{escape}label"
    assert blanking.blank(text, [value]) == expected


@contextmanager
def _page_showing(text: str, *, in_attributes: bool = False, in_form: bool = False):
    fields = (f'<input id="field" value="{html.escape(text)}" data-copy="{html.escape(text)}">'
              if in_attributes else "")
    if in_form:  # a GET form that submits the value to /done (served the same page)
        fields = (f'<form action="/done" method="get"><input type="hidden" name="user" value="me">'
                  f'<input id="field" name="p" value="{html.escape(text)}"><button id="send">Send</button></form>')
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
    return f"Ab1&\"<>'\u00a0{uuid.uuid4().hex}"


# Every way a browser writes a value into markup: Chrome 152's text and attribute serialisation (checked on a real
# page), XMLSerializer's (U+00A0 kept as it is), and attributes as written before Chrome escaped < and > in them.
MARKUP_FORMS = {
    "text": _chrome_text,
    "attribute": _chrome_attribute,
    "xml_text": lambda value: value.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;"),
    "xml_attribute": lambda value: (value.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
                                   .replace('"', "&quot;")),
    "attribute_before_lt_gt": lambda value: (value.replace("&", "&amp;").replace('"', "&quot;")
                                            .replace("\u00a0", "&nbsp;")),
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


# What Chrome 152's snapshot and page text make of a value's whitespace (measured on a real page): the saved value
# and the form shown. A snapshot drops U+00A0 from page text; page text collapses a run of spaces and trims spaces
# at either end; a snapshot -i line's end is trimmed of U+00A0 too.
WHITESPACE_FORMS = {
    "nbsp_dropped": ("Ab1\u00a0cd{m}", "Ab1cd{m}"),
    "spaces_collapsed": ("Ab1  cd{m}", "Ab1 cd{m}"),
    "leading_space_trimmed": (" Ab1cd{m}", "Ab1cd{m}"),
    "trailing_space_trimmed": ("Ab1cd{m} ", "Ab1cd{m}"),
    "trailing_nbsp_trimmed": ("Ab1cd{m}\u00a0", "Ab1cd{m}"),
    "all_at_once": ("  Ab1 \u00a0 cd{m}\u00a0", "Ab1 cd{m}"),
}


@pytest.mark.parametrize("in_json", [False, True])
@pytest.mark.parametrize("form", sorted(WHITESPACE_FORMS))
def test_each_whitespace_form_a_snapshot_shows_is_blanked(form, in_json):
    marker = uuid.uuid4().hex[:8]
    value, shown = (text.format(m=marker) for text in WHITESPACE_FORMS[form])
    assert shown != value
    if in_json:
        shown = json.dumps(shown)[1:-1]
    assert blanking.blank(f"<p>{shown}</p>", [value]) == f"<p>{blanking.BLANK}</p>"


def test_short_trimmed_form_is_hidden_as_a_whole_token():
    assert blanking._whitespace_forms("Ab1-cd-ef") == {"Ab1-cd-ef"}
    assert blanking.blank("a tab bar ab", ["ab      "]) == "a tab bar [hidden]"


@pytest.mark.integration
@pytest.mark.asyncio
@pytest.mark.parametrize("case", sorted(WHITESPACE_FORMS))
async def test_a_saved_value_with_whitespace_on_a_real_page_comes_back_blanked(state, monkeypatch, case):
    """Real Chrome: the value is page text and a shown-password text field's value; snapshot, snapshot -i and page
    text each come back with it blanked in whatever form they show its whitespace."""
    if os.environ.get("CORSIA_BROWSER_E2E") != "1":
        pytest.skip("set CORSIA_BROWSER_E2E=1 in the isolated host browser environment")
    marker = uuid.uuid4().hex
    shown_text = WHITESPACE_FORMS[case][0].format(m=marker)
    await asyncio.to_thread(saved_logins.save_login, site="https://example.com", username="me@example.com",
                            password=shown_text, saved_by="person")
    thread_id = f"blanking-{uuid.uuid4().hex}"
    owner = BrowserSessionOwner(namespace="corsia-blanking", instance_key=uuid.uuid4().hex)

    async def run_browser(thread, args, **kwargs):
        kwargs.pop("lease_id", None)
        return await owner.run(thread, args, **kwargs)

    monkeypatch.setattr(browser_runtime.PROCESS_OWNER, "run_browser", run_browser)
    commands = (["snapshot"], ["snapshot", "-i"], ["get", "text", "main"])
    try:
        with _page_showing(shown_text, in_attributes=True) as url:
            assert not (await browser_runtime._run(["open", url], thread_id=thread_id)).startswith("Error:")
            outputs = {" ".join(args): await browser_runtime._run(args, thread_id=thread_id) for args in commands}
    finally:
        await owner.close_thread(thread_id)
    for command, text in outputs.items():
        assert marker not in text, (command, text)
        assert blanking.BLANK in text, (command, text)


# A saved value with one character each URL encoder treats differently, and what the browser's form encoder
# (URLSearchParams, a submitted form) writes for it: written out by hand from the WHATWG URL standard and checked
# against Node's URLSearchParams, never built with the encoder under test.
FORM_ENCODED = {
    "tilde": ("Ab1~cdZq7x", "Ab1%7EcdZq7x"),
    "ampersand": ("Ab1&cdZq7x", "Ab1%26cdZq7x"),
    "plus": ("Ab1+cdZq7x", "Ab1%2BcdZq7x"),
    "space": ("Ab1 cdZq7x", "Ab1+cdZq7x"),
    "non_ascii": ("Ab1ücdZq7x", "Ab1%C3%BCcdZq7x"),
}
# Where a form-encoded value reaches an agent: a network log's URL and its postData, and get url after a GET form
# is submitted.
FORM_ENCODED_KINDS = {
    "network_url": lambda text: (0, {"success": True, "data": {"requests": [
        {"url": f"https://example.com/login?user=me&p={text}"}]}}),
    "post_data": lambda text: (0, {"success": True, "data": {"requests": [
        {"url": "https://example.com/login", "postData": f"user=me&p={text}"}]}}),
    "get_url": lambda text: (0, {"success": True, "data": {"url": f"https://example.com/done?user=me&p={text}"}}),
}


@pytest.mark.asyncio
@pytest.mark.parametrize("lowercase_hex", [False, True])
@pytest.mark.parametrize("kind", sorted(FORM_ENCODED_KINDS))
@pytest.mark.parametrize("case", sorted(FORM_ENCODED))
async def test_a_form_encoded_saved_value_is_blanked_wherever_it_comes_back(state, monkeypatch, case, kind,
                                                                            lowercase_hex):
    value, written = FORM_ENCODED[case]
    if lowercase_hex:  # as a page script may write it
        written = re.sub(r"%[0-9A-F]{2}", lambda match: match.group(0).lower(), written)
    saved_logins.save_login(site="https://example.com", username="me@example.com", password=value, saved_by="person")
    _fake_owner(monkeypatch, FORM_ENCODED_KINDS[kind](written))
    text = await browser_runtime._run(["get", "url"], thread_id="thread-1")
    assert written not in text and "Zq7x" not in text, text
    assert f"p={blanking.BLANK}" in text


# One value through each encoder the browser and page scripts use, as Node's WHATWG URL and JavaScript
# encoders write it: the form encoder, encodeURIComponent, encodeURI, the URL query serialiser (which leaves
# & = + / ? ` { } ^ and % as they are) and the path serialiser (the same value without ? and #, which end a path).
BROWSER_URL_VALUE = "Ab1 ~&+ü=/?#'\"<>`{}^%x"
BROWSER_URL_FORMS = {
    "form": (BROWSER_URL_VALUE, "Ab1+%7E%26%2B%C3%BC%3D%2F%3F%23%27%22%3C%3E%60%7B%7D%5E%25x"),
    "encode_uri_component": (BROWSER_URL_VALUE, "Ab1%20~%26%2B%C3%BC%3D%2F%3F%23'%22%3C%3E%60%7B%7D%5E%25x"),
    "encode_uri": (BROWSER_URL_VALUE, "Ab1%20~&+%C3%BC=/?#'%22%3C%3E%60%7B%7D%5E%25x"),
    "query": (BROWSER_URL_VALUE, "Ab1%20~&+%C3%BC=/?%23%27%22%3C%3E`{}^%x"),
    "path": ("Ab1 ~&+ü=/'\"<>`{}^%x", "Ab1%20~&+%C3%BC=/'%22%3C%3E%60%7B%7D%5E%x"),
}


@pytest.mark.parametrize("encoder", sorted(BROWSER_URL_FORMS))
def test_each_browser_url_encoding_of_a_value_is_blanked(encoder):
    value, written = BROWSER_URL_FORMS[encoder]
    assert blanking.blank(f"https://example.com/a?p={written}&next=1", [value]) == (
        f"https://example.com/a?p={blanking.BLANK}&next=1")


# A value with "?" and "#" written raw into a URL, and set as its fragment, as Node's WHATWG URL serialises it
# (new URL(base + value).href, and url.hash = value), checked against Node 25. Raw, the parser ends a path at "?"
# and a path or query at "#", and encodes each part with its own set, so no single-set form matches the whole.
HASH_VALUE = "A`b1 ?u'e{r}y^#`h a\"s<>#2%x"
HASH_FORMS = {
    "fragment": ("https://example.com/a#", "A%60b1%20?u'e{r}y^#%60h%20a%22s%3C%3E#2%x"),
    "raw_in_query": ("https://example.com/a?p=", "A`b1%20?u%27e{r}y^#%60h%20a%22s%3C%3E#2%x"),
    "raw_in_path": ("https://example.com/", "A%60b1%20?u%27e{r}y^#%60h%20a%22s%3C%3E#2%x"),
}


@pytest.mark.parametrize("where", sorted(HASH_FORMS))
def test_a_value_with_a_hash_written_into_a_url_is_blanked_whole(where):
    base, written = HASH_FORMS[where]
    # Whole: neither the part before "#" nor the one after is left behind.
    assert blanking.blank(f"{base}{written}", [HASH_VALUE]) == f"{base}{blanking.BLANK}"


def test_a_near_match_of_a_saved_value_is_left_alone():
    """Text one character off a saved value, or cut short, in any encoding, isn't blanked."""
    near = ("Ab1%7EcdZq7y", "Ab1~cdZq7y", "Ab1%7EcdZq7", "Ab1+cdZq7x", "Ab1%7ecdZq7y")
    for text in near:
        assert blanking.blank(f"p={text}", ["Ab1~cdZq7x"]) == f"p={text}", text


@pytest.mark.integration
@pytest.mark.asyncio
async def test_a_saved_value_submitted_in_a_real_get_form_comes_back_blanked(state, monkeypatch):
    """Real Chrome: a GET form submits the saved value (with ~, &, +, a space and a non-ASCII letter); get url comes
    back blanked, and so does each URL encoding the page's own script makes of it."""
    if os.environ.get("CORSIA_BROWSER_E2E") != "1":
        pytest.skip("set CORSIA_BROWSER_E2E=1 in the isolated host browser environment")
    marker = uuid.uuid4().hex
    form_value = f"Ab1 ~&+ü{marker}"
    await asyncio.to_thread(saved_logins.save_login, site="https://example.com", username="me@example.com",
                            password=form_value, saved_by="person")
    thread_id = f"blanking-{uuid.uuid4().hex}"
    owner = BrowserSessionOwner(namespace="corsia-blanking", instance_key=uuid.uuid4().hex)

    async def run_browser(thread, args, **kwargs):
        kwargs.pop("lease_id", None)
        return await owner.run(thread, args, **kwargs)

    monkeypatch.setattr(browser_runtime.PROCESS_OWNER, "run_browser", run_browser)
    encodings_js = ("(() => { const v = document.getElementById('field').value; return [new URLSearchParams({p: v})"
                    ".toString(), encodeURIComponent(v), encodeURI(v), new URL('https://example.com/a?p=' + v).search, "
                    "new URL('https://example.com/' + v).pathname].join(' | '); })()")
    try:
        with _page_showing(form_value, in_form=True) as url:
            assert not (await browser_runtime._run(["open", url], thread_id=thread_id)).startswith("Error:")
            encoded = await browser_runtime._run(["eval", encodings_js], thread_id=thread_id)
            submitted = await browser_runtime._run(["click", "#send"], thread_id=thread_id)
            assert not submitted.startswith("Error:"), submitted
            await browser_runtime._run(["wait", "--load", "networkidle"], thread_id=thread_id)
            current = await browser_runtime._run(["get", "url"], thread_id=thread_id)
    finally:
        await owner.close_thread(thread_id)
    assert "/done?" in current, current
    for text in (encoded, current):
        assert marker not in text, text
        assert blanking.BLANK in text, text
    assert encoded.count(blanking.BLANK) == 5, encoded


def test_every_written_form_of_a_value_is_blanked_longest_first():
    value = "abc/def ghi\"jkl"
    text = " | ".join(sorted(blanking.encodings(value)))
    assert set(blanking.blank(text, [value]).split(" | ")) == {blanking.BLANK}


@pytest.mark.parametrize("written", ["123456789", "123 45 6789", "123.45.6789", "1234-56789", "1  23/45-6789",
                                     "123%2045%206789", "123+45+6789", "123%2F45%2f6789", r"123\/45\/6789"])
def test_grouped_number_is_hidden_in_any_grouping(written):
    assert blanking.blank(f"({written})", ["123-45-6789"]) == "([hidden])"


@pytest.mark.parametrize("value,written", [("1234 5678 9012 3456", "1234567890123456"),
                                           ("987/654/321", "987-65-4321")])
def test_card_and_account_groupings(value, written):
    assert blanking.blank(written, [value]) == blanking.BLANK


@pytest.mark.parametrize("written", ["١٢٣", "%D9%A1%20%D9%A2%D9%A3", r"\u0661-\u0662\u0663"])
def test_encoded_unicode_digit_groupings(written):
    assert blanking.blank(f"({written})", ["١-٢٣"]) == "([hidden])"


def test_token_boundaries_and_non_alphanumeric_edges():
    assert blanking.blank("a1 xa1 a1x éa1 a1٣ _a1_ !a1!", ["a1"]) == (
        "[hidden] xa1 a1x éa1 a1٣ _[hidden]_ ![hidden]!")
    assert blanking.blank("x!a1!y", ["!a1!"]) == "x[hidden]y"
    assert blanking.blank("1234567890 6789", ["123-45-6789"]) == "1234567890 6789"


@pytest.mark.parametrize("value,written", [("a~b", "a%7Eb"), ("a\"b", r'a\"b'), ("a&b", "a&amp;b")])
def test_short_encoded_values(value, written):
    assert blanking.blank(f"({written})", [value]) == "([hidden])"


@pytest.mark.parametrize("base,value,written", [
    ("https://example.com/a?p=", "a~b", "a%7Eb"),
    ("https://example.com/a?p=", "abc~defgh", "abc%7Edefgh"),
    ("https://example.com/a?p=", "a# b", "a#%20b"),
    ("https://example.com/a?p=", "abcdef# gh", "abcdef#%20gh"),
    ("https://example.com/", "a? b", "a?%20b"),
    ("https://example.com/", "abcdef? gh", "abcdef?%20gh"),
    ("https://example.com/a#", "a`b", "a%60b"),
    ("https://example.com/a#", "abcdef`gh", "abcdef%60gh"),
])
def test_short_and_long_browser_url_forms(base, value, written):
    assert blanking.blank(f"{base}{written}", [value]) == f"{base}[hidden]"


@pytest.mark.parametrize("value,written", [("abc ", "abc"), ("abcdefgh ", "abcdefgh"),
                                           (" abc", "%20abc"), (" abcdefgh", "%20abcdefgh"),
                                           ("a b ", "a%20b")])
def test_url_ending_with_a_value_whose_edge_space_is_serialized_or_trimmed(value, written):
    assert blanking.blank(f"https://example.com/{written}", [value]) == "https://example.com/[hidden]"


def test_blanking_large_page_with_many_values_is_fast():
    values = [f"secret-{n:04d}-value" for n in range(200)]
    page = ("ordinary page content, no saved value. " * 27000)[:999_982] + " " + values[137] + " "
    started = time.perf_counter()
    assert blanking.blank(page, values) == page.replace(values[137], blanking.BLANK)
    assert time.perf_counter() - started < 3.0
