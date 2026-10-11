"""Deterministic redaction for recognized credentials on model-visible surfaces.

This is intentionally a narrow sanitizer, not arbitrary data-loss prevention.
It preserves surrounding text and structure while removing credential values
in explicit headers, assignments, URLs, cookies, and private-key blocks.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping
import re
from typing import Any


REDACTION_MARKER = "[REDACTED]"

_CREDENTIAL_KEY = (
    r"(?:pgpassword|"
    r"(?:[A-Za-z0-9]+[-_])*(?:"
    r"api[-_]?key|token|secret|password|passwd|credentials?|"
    r"private[-_]?key|secret[-_]access[-_]key))"
)
_SENSITIVE_KEY_RE = re.compile(
    rf"^(?:{_CREDENTIAL_KEY}|authorization|cookie)$",
    re.IGNORECASE,
)
_TEXT_ASSIGNMENT_KEY = _CREDENTIAL_KEY
_ASSIGNMENT_RE = re.compile(
    rf"(?P<prefix>(?<![A-Za-z0-9_-])"
    rf"(?:(?P<key_quote>[\"']){_TEXT_ASSIGNMENT_KEY}(?P=key_quote)"
    rf"|{_TEXT_ASSIGNMENT_KEY})\s*[:=]\s*)"
    r"(?:(?P<quote>[\"'])(?P<quoted>.*?)(?P=quote)"
    r"|(?P<bare>[^\s,;&}\]]+))",
    re.IGNORECASE,
)
_COOKIE_ASSIGNMENT_RE = re.compile(
    r"(?P<prefix>(?<![A-Za-z0-9_-])cookie\s*=\s*)"
    r"(?:(?P<quote>[\"'])(?P<quoted>.*?)(?P=quote)"
    r"|(?P<bare>[^\s,;&}\]]+))",
    re.IGNORECASE,
)
_AUTH_SCHEME_RE = re.compile(
    r"(?P<prefix>\b(?:(?:proxy-)?authorization|auth)\s*[:=]\s*"
    r"(?:bearer|basic)\s+)"
    r"(?P<value>(?!\[REDACTED\])[^\s\"'`,;}\]]+)",
    re.IGNORECASE,
)
_AUTH_SPACE_SCHEME_RE = re.compile(
    r"(?P<prefix>\bauthorization\s+(?:bearer|basic)\s+)"
    r"(?P<value>(?!\[REDACTED\])[^\s\"'`,;}\]]+)",
    re.IGNORECASE,
)
_NETRC_PASSWORD_RE = re.compile(
    r"(?P<prefix>\b(?:machine\s+\S+|default)\s+"
    r"(?:login\s+\S+\s+)?password\s+)"
    r"(?P<value>(?!\[REDACTED\])[^\s\"'`,;}\]]+)",
    re.IGNORECASE,
)
_OAUTH_TOKEN_RE = re.compile(
    r"(?P<prefix>(?<![A-Za-z0-9_-])oauth[-_]?token\s+)"
    r"(?P<value>(?!\[REDACTED\])[^\s\"'`,;}\]]+)",
    re.IGNORECASE,
)
_AUTH_VALUE_RE = re.compile(
    r"(?P<scheme>bearer|basic)\s+(?P<value>.+)",
    re.IGNORECASE,
)
_COOKIE_HEADER_RE = re.compile(
    r"(?P<boundary>^|[^A-Za-z0-9_-]|\\[rn])"
    r"(?P<prefix>(?P<kind>set-cookie|cookie)\s*:\s*)"
    r"(?P<value>[^\r\n\"']*)",
    re.IGNORECASE,
)
_URL_USERINFO_RE = re.compile(
    r"(?P<scheme>\b[A-Za-z][A-Za-z0-9+.-]*://)"
    r"(?P<userinfo>[^\s/@]+)@",
)
_PEM_PRIVATE_KEY_RE = re.compile(
    r"(?P<begin>-----BEGIN (?P<label>(?:[A-Z0-9]+ )*PRIVATE KEY)-----)"
    r".*?"
    r"(?:(?P<end>-----END (?P=label)-----)|\Z)",
    re.DOTALL,
)
_PLACEHOLDERS = frozenset(
    {
        "",
        "none",
        "null",
        "unset",
        "missing",
        "<unset>",
        "<missing>",
        "example",
        "sample",
        "dummy",
        "test",
        "changeme",
        REDACTION_MARKER.casefold(),
    }
)


class CredentialSafeText(str):
    """Text already redacted by a source-aware boundary."""


def _is_placeholder(value: str) -> bool:
    return value.strip().casefold() in _PLACEHOLDERS


def _is_credential_key(name: object) -> bool:
    """Whether a binding name is a recognized credential key."""
    return isinstance(name, str) and bool(_SENSITIVE_KEY_RE.fullmatch(name.strip()))


def _redact_assignment(match: re.Match[str]) -> str:
    value = match.group("quoted")
    if value is None:
        value = match.group("bare") or ""
    if _is_placeholder(value):
        return match.group(0)
    quote = match.group("quote") or ""
    return f"{match.group('prefix')}{quote}{REDACTION_MARKER}{quote}"


def _redact_cookie_header(match: re.Match[str]) -> str:
    segments = match.group("value").split(";")
    limit = 1 if match.group("kind").casefold() == "set-cookie" else len(segments)
    rendered: list[str] = []
    for index, segment in enumerate(segments):
        key, separator, value = segment.partition("=")
        if (
            index < limit
            and separator
            and not _is_placeholder(value)
        ):
            segment = f"{key}={REDACTION_MARKER}"
        rendered.append(segment)
    return match.group("boundary") + match.group("prefix") + ";".join(rendered)


def _redact_url_userinfo(match: re.Match[str]) -> str:
    scheme = match.group("scheme")
    transport = scheme[:-3].rsplit("+", 1)[-1].casefold()
    userinfo = match.group("userinfo")
    identity, separator, _credential = userinfo.partition(":")
    if not separator:
        if transport in {"http", "https"}:
            return f"{scheme}{REDACTION_MARKER}@"
        return match.group(0)
    safe_userinfo = f"{identity}:{REDACTION_MARKER}"
    return f"{scheme}{safe_userinfo}@"


def _redact_private_key(match: re.Match[str]) -> str:
    end = match.group("end")
    suffix = f"\n{end}" if end else ""
    return f"{match.group('begin')}\n{REDACTION_MARKER}{suffix}"


def _redact_space_value(match: re.Match[str]) -> str:
    if _is_placeholder(match.group("value")):
        return match.group(0)
    return match.group("prefix") + REDACTION_MARKER


def _redact_auth_scheme(match: re.Match[str]) -> str:
    return match.group("prefix") + REDACTION_MARKER


def _redact_token_prefix(_match: re.Match[str]) -> str:
    return REDACTION_MARKER


_TOKEN_PREFIX_RE = re.compile(
    r"\b(?:sk-(?:ant-)?|ghp_|gho_|github_pat_|glpat-|xox[abpr]-|AIza|ya29\.|dop_v1_)"
    r"[A-Za-z0-9_-]{16,}|\b(?:AKIA|ASIA)[0-9A-Z]{12,}\b"
)

# Applied in this order; sanitize_credential_text and credential_spans share it.
_RULES: tuple[tuple[re.Pattern[str], Callable[[re.Match[str]], str]], ...] = (
    (_PEM_PRIVATE_KEY_RE, _redact_private_key),
    (_URL_USERINFO_RE, _redact_url_userinfo),
    (_COOKIE_HEADER_RE, _redact_cookie_header),
    (_AUTH_SCHEME_RE, _redact_auth_scheme),
    (_AUTH_SPACE_SCHEME_RE, _redact_space_value),
    (_NETRC_PASSWORD_RE, _redact_space_value),
    (_OAUTH_TOKEN_RE, _redact_space_value),
    (_ASSIGNMENT_RE, _redact_assignment),
    (_COOKIE_ASSIGNMENT_RE, _redact_assignment),
    (_TOKEN_PREFIX_RE, _redact_token_prefix),
)
# Rules that recognize a credential by its own text, not by a key or header
# around it. Their matches stay secret whatever the text parses as.
_SHAPE_PATTERNS = frozenset({_PEM_PRIVATE_KEY_RE, _TOKEN_PREFIX_RE})
# Rules whose matches are never code or prose, whatever the text lexes as: a
# credential's own shape, URL userinfo, and cookie values.
_NEVER_CODE_PATTERNS = _SHAPE_PATTERNS | {
    _URL_USERINFO_RE, _COOKIE_HEADER_RE, _COOKIE_ASSIGNMENT_RE,
}


def sanitize_credential_text(value: str) -> str:
    """Redact recognized credential values while retaining useful context."""
    if not isinstance(value, str) or not value:
        return value
    for pattern, callback in _RULES:
        value = pattern.sub(callback, value)
    return value


def credential_spans(
    value: str, *, shape_only: bool = False, never_code: bool = False,
) -> list[tuple[int, int]]:
    """Merged regions of ``value`` that the rules would change.

    Each rule runs on the original text, so a region is reported even when an
    earlier rule would already have rewritten it. ``shape_only`` limits the
    regions to rules that recognize a credential by its own text;
    ``never_code`` adds URL userinfo and cookie values to those.
    """
    selected = _SHAPE_PATTERNS if shape_only else _NEVER_CODE_PATTERNS if never_code else None
    spans: list[tuple[int, int]] = []
    for pattern, callback in _RULES:
        if selected is not None and pattern not in selected:
            continue
        for match in pattern.finditer(value):
            old, new = match.group(0), callback(match)
            limit = min(len(old), len(new))
            # Trim only text outside the markers: a value whose edge happens
            # to equal a marker character is still part of the span.
            first = new.find(REDACTION_MARKER)
            if first >= 0:
                head_limit = first
                tail_limit = len(new) - new.rfind(REDACTION_MARKER) - len(REDACTION_MARKER)
            else:
                head_limit = tail_limit = limit
            head = 0
            while head < min(limit, head_limit) and old[head] == new[head]:
                head += 1
            tail = 0
            while tail < min(limit - head, tail_limit) and old[-1 - tail] == new[-1 - tail]:
                tail += 1
            if head + tail < len(old):
                spans.append((match.start() + head, match.end() - tail))
    merged: list[tuple[int, int]] = []
    for start, end in sorted(spans):
        if merged and start <= merged[-1][1]:
            merged[-1] = (merged[-1][0], max(merged[-1][1], end))
        else:
            merged.append((start, end))
    return merged


def contains_credential(value: str) -> bool:
    """Whether text carries a recognized credential; hex digests and plain URLs do not count."""
    return bool(value) and sanitize_credential_text(value) != value


def sanitize_credential_data(value: Any) -> Any:
    """Recursively sanitize strings and explicit credential-keyed values."""
    if isinstance(value, str):
        return sanitize_credential_text(value)
    if isinstance(value, Mapping):
        safe: dict[Any, Any] = {}
        for key, child in value.items():
            sanitized = sanitize_credential_data(child)
            normalized_key = (
                key.strip().strip("\"'").casefold()
                if isinstance(key, str)
                else ""
            )
            if (
                isinstance(key, str)
                and _is_credential_key(normalized_key)
                and child is not None
                and not isinstance(child, (Mapping, list, tuple))
            ):
                if isinstance(child, str):
                    sanitized = sanitize_credential_text(child)
                    authorization = (
                        _AUTH_VALUE_RE.fullmatch(child.strip())
                        if normalized_key == "authorization"
                        else None
                    )
                    if (
                        authorization is not None
                        and not _is_placeholder(
                            authorization.group("value")
                        )
                    ):
                        sanitized = (
                            f"{authorization.group('scheme')} "
                            f"{REDACTION_MARKER}"
                        )
                    if sanitized == child and not _is_placeholder(child):
                        sanitized = REDACTION_MARKER
                else:
                    sanitized = REDACTION_MARKER
            safe[key] = sanitized
        return safe
    if isinstance(value, list):
        return [sanitize_credential_data(item) for item in value]
    if isinstance(value, tuple):
        return tuple(sanitize_credential_data(item) for item in value)
    return value
