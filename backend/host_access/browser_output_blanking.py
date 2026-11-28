"""Blank every saved password out of what the browser returns to an agent (C6).

Every agent browser command's text comes back through ``tools.browser_runtime._run_owned``: its result,
and the text of any error it raised. That one place calls :func:`blank_output` once per command, which reads
the saved passwords once (``infra.saved_logins.blanking_values``) and replaces each form, raw, URL-encoded,
HTML-escaped or JSON-encoded, and with its whitespace as a snapshot or page text shows it, with :data:`BLANK`.
Text forms match whole tokens; number-shaped values match other digit groupings with digit boundaries.
That covers snapshots, page text, `get` results (get html too), eval and script results, console and network
logs, and error text.

A result that can't be checked, because the saved passwords couldn't be read, is withheld rather than returned
unchecked. Nothing here logs or returns a password: the values may remain in Python's regex cache.
"""
from __future__ import annotations

import json
import logging
import re
from typing import Callable, Iterable
from urllib.parse import quote, quote_plus

from infra import saved_logins
from infra.database import get_db

log = logging.getLogger(__name__)

BLANK = "[hidden]"
UNCHECKED = "Error: this browser result was withheld because it couldn't be checked for saved passwords."
# The characters each URL encoder leaves as they are: Python's quote with nothing safe, JavaScript's
# encodeURIComponent and encodeURI, the form encoder (URLSearchParams) and a path encoder that keeps "/".
# Python's quote never encodes letters, digits or "-_.~", so the browser's encoders are written out below.
_URL_SAFE = ("", "-_.!~*'()", "-_.!~*'();,/?:@&=+$#", "-_.*", "/")
# What the browser's own serialisers (the WHATWG URL standard) encode besides controls, DEL and non-ASCII:
# the query, the path with and without "^", which the standard added later, and the fragment (the part after
# "#"). Everything else stays as it is.
_QUERY, _PATH, _PATH_BEFORE_CARET, _FRAGMENT = ' "#<>\'', ' "#<>?`{}^', ' "#<>?`{}', ' "<>`'
_BROWSER_ENCODED = (_QUERY, _PATH, _PATH_BEFORE_CARET, _FRAGMENT)
# The form encoder (URLSearchParams and a submitted form) keeps only letters, digits and "*-._", so it writes
# "~" as %7E, and a space as "+".
_FORM_KEPT = frozenset("*-._")
_PERCENT = re.compile(r"%[0-9A-F]{2}")
# Page text's collapsible whitespace: a run of these shows as one space.
_COLLAPSIBLE = re.compile(r"[ \t\n\r\f]+")
_ASCII_SPACE = " \t\n\r\f"


def _percent_encode(value: str, encoded: Callable[[str], bool]) -> str:
    return "".join("".join(f"%{byte:02X}" for byte in char.encode()) if encoded(char) else char for char in value)


def _serialiser(chars: str) -> Callable[[str], bool]:
    return lambda char: not " " < char < "\x7f" or char in chars


def _parsed(value: str, path: str | None) -> str:
    """The value written raw into a URL's path (encoded with `path`) or, when `path` is None, its query, as the
    browser's parser serialises it: "?" moves a path on to the query and "#" moves either on to the fragment.
    Each separator stays as it is, and each part is encoded with its own set."""
    sets = {"path": path or "", "query": _QUERY, "fragment": _FRAGMENT}
    part, out = ("path" if path is not None else "query"), []
    for char in value:
        if (char == "?" and part == "path") or (char == "#" and part != "fragment"):
            part = "query" if char == "?" else "fragment"
            out.append(char)
        else:
            out.append(_percent_encode(char, _serialiser(sets[part])))
    return "".join(out)


def _browser_url_forms(value: str) -> set[str]:
    forms = {_percent_encode(value, _serialiser(chars)) for chars in _BROWSER_ENCODED}
    forms |= {_parsed(value, path) for path in (_PATH, _PATH_BEFORE_CARET, None)}
    form = _percent_encode(value, lambda char: char != " " and not (
        char.isascii() and (char.isalnum() or char in _FORM_KEPT)))
    return forms | {form.replace(" ", "+")}


def _url_forms(value: str) -> set[str]:
    forms = {quote(value, safe=safe) for safe in _URL_SAFE} | {quote_plus(value, safe=safe) for safe in _URL_SAFE}
    forms |= _browser_url_forms(value)
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


def _whitespace_forms(value: str) -> set[str]:
    """The value as a snapshot or page text shows it (Chrome 152, checked on a real page): a snapshot's page text
    drops U+00A0; page text collapses a run of spaces to one and trims spaces at either end; a snapshot line's
    end is trimmed of U+00A0 too. Any mix of these."""
    forms, todo = {value}, [value]
    while todo:
        text = todo.pop()
        for form in (text.replace("\u00a0", ""), _COLLAPSIBLE.sub(" ", text), text.strip(_ASCII_SPACE), text.strip()):
            if form not in forms:
                forms.add(form)
                todo.append(form)
    return forms


def encodings(value: str) -> set[str]:
    """Every written form of one saved value that is blanked: raw, URL-encoded, HTML-escaped, as a snapshot or
    page text shows its whitespace, and each of those JSON-encoded (get html and get text return theirs inside
    JSON). URLs and markup keep whitespace as written; only rendered text changes it."""
    whitespace = _whitespace_forms(value)
    plain = whitespace | {form for text in whitespace for form in _url_forms(text) | _html_forms(text)}
    return {form for text in plain for form in {text} | _json_forms(text) if form}


def _bounded(form: str, pattern: str) -> str:
    # Python's Unicode word characters except underscore are precisely letters and digits.
    return pattern + (r"(?![^\W_])" if form[-1].isalnum() else "")


def _digit_forms(digits: str) -> set[tuple[str, ...]]:
    transforms = (lambda char: char, lambda char: quote(char, safe=""),
                  lambda char: quote(char, safe="").lower(), lambda char: json.dumps(char)[1:-1],
                  lambda char: json.dumps(json.dumps(char)[1:-1])[1:-1])
    return {tuple(transform(char) for char in digits) for transform in transforms}


def blank(text: str, values: Iterable[str]) -> str:
    """Hide whole-token text and digit-bounded numeric forms, longest first."""
    forms = {form for value in values for form in encodings(value)}
    patterns: dict[bool, set[tuple[int, str]]] = {True: set(), False: set()}
    numbers: dict[bool, set[tuple[int, str]]] = {True: set(), False: set()}
    for form in forms:
        patterns[form[0].isalnum()].add((len(form), _bounded(form, re.escape(form))))
        if all(char.isdigit() or char in " -./" for char in form):
            digits = "".join(char for char in form if char.isdigit())
            if digits:
                for encoded in _digit_forms(digits):
                    pattern = r"(?: *|[-./]|\++|(?:%20)+|%2[fF]|\\{1,2}/)".join(map(re.escape, encoded))
                    edge = encoded[0] + encoded[-1]
                    suffix = r"(?!\d)" if edge[-1].isdigit() else ""
                    numbers[edge[0].isdigit()].add((len("".join(encoded)), pattern + suffix))
    groups = []
    # Match a value's own encoded prefix before consuming an escape as its boundary.
    for bounded in (False, True):
        entries = patterns[bounded]
        if entries:
            prefix = (r"(?:(?<![^\W_])|(?P<escape>\\(?:[nrtbfv0]|x[0-9A-Fa-f]{2}|u[0-9A-Fa-f]{4}"
                      r"|U[0-9A-Fa-f]{8})|%(?:25)*[0-9A-Fa-f]{2}))") if bounded else ""
            groups.append(prefix + "(?:" + "|".join(p for _, p in sorted(entries, reverse=True)) + ")")
    for bounded in (False, True):
        if numbers[bounded]:
            prefix = (r"(?:(?<!\d)|(?P<number_escape>\\(?:[nrtbfv0]|x[0-9A-Fa-f]{2}|u[0-9A-Fa-f]{4}"
                      r"|U[0-9A-Fa-f]{8})|%(?:25)*[0-9A-Fa-f]{2}))") if bounded else ""
            groups.append(prefix + "(?:" + "|".join(p for _, p in sorted(numbers[bounded], reverse=True)) + ")")
    return re.sub("|".join(groups), lambda m: (m.groupdict().get("escape") or m.groupdict().get("number_escape") or "")
                  + BLANK, text) if groups else text


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
