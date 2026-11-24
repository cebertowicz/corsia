"""Blank every saved password out of what the browser returns to an agent (C6).

Every agent browser command's text comes back through ``tools.browser_runtime._run_owned``: its result,
and the text of any error it raised. That one place calls :func:`blank_output` once per command, which reads
the saved passwords once (``infra.saved_logins.blanking_values``: each saved password of 8 or more characters)
and replaces each one wherever it appears, raw, URL-encoded, HTML-escaped or JSON-encoded, with :data:`BLANK`.
That covers snapshots, page text, `get` results (get html too), eval and script results, console and network
logs, and error text.

A result that can't be checked, because the saved passwords couldn't be read, is withheld rather than returned
unchecked. Nothing here logs, keeps or returns a password: the values live only in this call's locals.
"""
from __future__ import annotations

import json
import logging
import re
from typing import Iterable
from urllib.parse import quote, quote_plus

from infra import saved_logins
from infra.database import get_db

log = logging.getLogger(__name__)

BLANK = "[hidden]"
UNCHECKED = "Error: this browser result was withheld because it couldn't be checked for saved passwords."
# The characters each URL encoder leaves as they are: Python's quote with nothing safe, JavaScript's
# encodeURIComponent, the form encoder (URLSearchParams) and a path encoder that keeps "/".
_URL_SAFE = ("", "-_.!~*'()", "-_.*", "/")
_PERCENT = re.compile(r"%[0-9A-F]{2}")


def _url_forms(value: str) -> set[str]:
    forms = {quote(value, safe=safe) for safe in _URL_SAFE} | {quote_plus(value, safe=safe) for safe in _URL_SAFE}
    # A percent escape may be written in lowercase hex (%2f), as some encoders do.
    return forms | {_PERCENT.sub(lambda match: match.group(0).lower(), form) for form in forms}


def _json_forms(value: str) -> set[str]:
    """The value inside a JSON string, escaped once or twice, each time with non-ASCII escaped or kept, and "/"
    as "\\/"."""
    forms = set()
    for first in (True, False):
        once = json.dumps(value, ensure_ascii=first)[1:-1]
        forms |= {once, once.replace("/", "\\/")}
        for second in (True, False):
            forms.add(json.dumps(once, ensure_ascii=second)[1:-1])
    return forms


def _html_forms(value: str) -> set[str]:
    """The value as a browser serialises it into markup (Chrome 152, checked on a real page): text escapes &, <, >
    and U+00A0 as &nbsp;; an attribute also escapes " as &quot;. XMLSerializer keeps U+00A0 as it is, and
    attribute serialisation before Chrome escaped < and > there kept them, so both are written too."""
    escaped = value.replace("&", "&amp;")
    forms = set()
    for markup in (escaped.replace("<", "&lt;").replace(">", "&gt;"), escaped):
        for quoted in (markup, markup.replace('"', "&quot;")):
            forms |= {quoted, quoted.replace("\u00a0", "&nbsp;")}
    return forms


def encodings(value: str) -> set[str]:
    """Every written form of one saved value that is blanked: raw, URL-encoded, HTML-escaped and each of those
    JSON-encoded (get html returns its markup inside JSON)."""
    plain = {value} | _url_forms(value) | _html_forms(value)
    return {form for text in plain for form in {text} | _json_forms(text) if form}


def blank(text: str, values: Iterable[str]) -> str:
    """The text with every form of each value replaced by BLANK, the longest forms first.

    One str.replace per form, deliberately: measured on a 200 KB snapshot, about 14 ms at 100 saved logins and
    143 ms at 1000. A single compiled alternation was 4 to 8 times slower, and a trie-shaped one is faster
    only when kept between commands, which would hold every saved password's forms in memory.
    """
    forms = sorted({form for value in values for form in encodings(value)}, key=len, reverse=True)
    for form in forms:
        text = text.replace(form, BLANK)
    return text


def _saved_values() -> frozenset[str]:
    db = get_db()
    try:
        return saved_logins.blanking_values(db=db)
    finally:
        db.close()


def blank_output(text: str) -> str:
    """One command's text with every saved password blanked; withheld when the saved passwords can't be read."""
    try:
        values = _saved_values()
    except Exception:  # noqa: BLE001 - any failure withholds the result; the text is fixed
        log.warning("browser output withheld: saved passwords couldn't be read for blanking")
        return UNCHECKED
    return blank(text, values) if values else text
