"""Credentials inside a larger word: embedded URLs and expression text.

The redactor in ``host_access.redaction`` decides what a URL or a key
holds; this module only finds where, in one shell word, a URL or a
credential-named key's value sits.
"""

from __future__ import annotations

import re
from typing import Callable

_URL_START_RE = re.compile(r"[A-Za-z][A-Za-z0-9+.-]*://")
_EMBEDDED_URL_RE = re.compile(r"[A-Za-z][A-Za-z0-9+.-]*://[^\s'\"`<>]*")
# A key, quoted or bare, then the first character of what follows it, past
# any ``:`` or ``=``. A bare name right after ``.`` or ``$`` is a jq path
# or a variable, not a key.
_KEY_VALUE_RE = re.compile(
    r"(?P<key>\"[^\"\n]*\"|'[^'\n]*'|(?<![\w.$-])[A-Za-z_][\w-]*(?![\w-]))"
    # The value is only looked at, so it can still start the next key.
    r"\s*[:=]?\s*(?=(?P<value>\S))"
)
# A value that starts with one of these is a jq path (``.x``), a variable
# (``$x``) or an expression, or there is no value; anything else (a quoted
# string, a bare word) is a literal.
_NOT_LITERAL_STARTS = frozenset(".$([{@,;|)]}")

Redaction = tuple[str, int, int]


def redact_url_text(value: str, redact_url: Callable[[str], Redaction]) -> Redaction:
    """Redact each URL in ``value`` with ``redact_url``.

    A word, or the value after ``key=``, that starts with ``scheme://`` is
    one URL, and ``redact_url`` raises when it is invalid. A URL inside
    other text (a ``python -c`` string) is found by its ``scheme://`` span;
    an invalid span stays text, unless it has an ``@`` where a password
    could sit, which fails closed.
    """
    if "://" not in value:
        return value, 0, 0
    if _URL_START_RE.match(value):
        return redact_url(value)
    key, separator, assigned = value.partition("=")
    if separator and _URL_START_RE.match(assigned):
        rendered, count, literals = redact_url(assigned)
        return f"{key}={rendered}", count, literals

    redactions = literals = 0

    def replace(match: re.Match[str]) -> str:
        nonlocal redactions, literals
        span = match.group()
        try:
            rendered, count, span_literals = redact_url(span)
        except ValueError:
            if "@" in span:
                raise
            return span
        redactions += count
        literals += span_literals
        return rendered if count else span

    rendered = _EMBEDDED_URL_RE.sub(replace, value)
    if not redactions:
        return value, 0, 0
    return rendered, redactions, literals


def has_literal_key_value(
    text: str, is_sensitive_key: Callable[[str], bool]
) -> bool:
    """Whether a credential-named key in ``text`` is given a literal value.

    A literal is a quoted string or a bare word. A jq path, a variable or
    an expression after the key (``{token: .x}``) is not a credential.
    """
    for match in _KEY_VALUE_RE.finditer(text):
        key = match.group("key").strip("\"'")
        if not is_sensitive_key(key):
            continue
        if match.group("value") not in _NOT_LITERAL_STARTS:
            return True
    return False
