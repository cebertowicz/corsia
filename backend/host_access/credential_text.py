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
# A key, quoted or bare, past any ``:`` or ``=``. A bare name right after
# ``.`` or ``$`` is a jq path or a variable, not a key.
_KEY_VALUE_RE = re.compile(
    r"(?P<key>\"[^\"\n]*\"|'[^'\n]*'|(?<![\w.$-])[A-Za-z_][\w-]*(?![\w-]))"
    r"\s*[:=]?\s*"
)
# What a value may hold and still be an expression: jq paths, variables,
# numbers, arithmetic, pipes, grouping and ``,`` inside it, and a value the
# redactor already replaced with its marker (a descriptor checked again). Anything else (a
# quote, a bare word, ``@format``) is literal text.
_EXPRESSION_PIECE_RE = re.compile(
    r"\s+|(?P<ref>\.(?:[A-Za-z_]\w*)?|\$[A-Za-z_]\w*|\[REDACTED\])"
    r"|\d+(?:\.\d+)?|//|[-+*/%|,()\[\]]"
)

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
    The whole value, up to the next ``,``, ``}`` or ``;`` outside brackets,
    is judged: a quoted string or a bare word anywhere in it is a literal.
    """
    for match in _KEY_VALUE_RE.finditer(text):
        key = match.group("key").strip("\"'")
        if is_sensitive_key(key) and not _is_expression(
            _value_span(text, match.end())
        ):
            return True
    return False


def _value_span(text: str, start: int) -> str:
    """The value from ``start`` to its end: ``,``, ``}`` or ``;`` at
    bracket depth zero, or a closer with no opener in the value."""
    depth = 0
    for index in range(start, len(text)):
        char = text[index]
        if char in "([":
            depth += 1
        elif depth:
            depth -= char in ")]"
        elif char in ")],};":
            return text[start:index]
    return text[start:]


def _is_expression(value: str) -> bool:
    """Whether ``value`` is empty or only paths, variables and arithmetic
    that names at least one of them."""
    position = refs = 0
    while position < len(value):
        piece = _EXPRESSION_PIECE_RE.match(value, position)
        if piece is None:
            return False
        refs += piece.group("ref") is not None
        position = piece.end()
    return refs > 0 or not value.strip()
