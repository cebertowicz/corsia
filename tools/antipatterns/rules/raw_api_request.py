"""Detect a frontend request to `/api` that skips the shared request function."""

from __future__ import annotations

import re
from typing import Iterable

from ..config import rule_settings
from ..findings import Finding
from ._base import Rule, RuleContext
from .periodic_server_reread import _balanced, _blank, _function_body


_RULE_ID = "raw-api-request"

_FETCH_CALL = re.compile(r"(?<![\w$.])(?:(?:window|globalThis|self)\.)?fetch\s*\(")
_EVENT_SOURCE = re.compile(r"\bnew\s+EventSource\s*\(")
_XHR_OPEN = re.compile(r"\.open\s*\(")
_API_FETCH = re.compile(r"(?<![\w$.])apiFetch\s*\(")
_SESSION_PARAM_SET = re.compile(r"\.(?:set|append)\s*\(\s*['\"]token['\"]")
_SESSION_IN_ADDRESS = re.compile(r"[?&]token=")
_IDENT = re.compile(r"^[A-Za-z_$][\w$]*$")
_CALL = re.compile(r"^([A-Za-z_$][\w$]*)\s*\(")


def _is_test_file(path: str) -> bool:
    return (
        path.endswith((".test.ts", ".spec.ts"))
        or "/__tests__/" in f"/{path}"
    )


def _argument(text: str, open_paren: int, index: int) -> tuple[int, int]:
    """Offsets of the top-level argument number `index` of the call opened at `open_paren`.

    `text` is the blanked source, so brackets inside strings and comments do
    not count. Returns (0, 0) when the call has no such argument.
    """
    end = _balanced(text, open_paren)
    if end == -1:
        return 0, 0
    depth, start, seen = 0, open_paren + 1, 0
    for i in range(open_paren + 1, end - 1):
        c = text[i]
        if c in "([{":
            depth += 1
        elif c in ")]}":
            depth -= 1
        elif c == "," and depth == 0:
            if seen == index:
                return start, i
            seen, start = seen + 1, i + 1
    return (start, end - 1) if seen == index else (0, 0)


def _initializer(source: str, name: str) -> str:
    """Text of every same-file `const|let|var name = ...` initializer, joined."""
    parts: list[str] = []
    for m in re.finditer(rf"\b(?:const|let|var)\s+{re.escape(name)}\s*(?::[^=]+)?=\s*", source):
        depth, i = 0, m.end()
        while i < len(source):
            c = source[i]
            if c in "([{":
                depth += 1
            elif c in ")]}":
                depth -= 1
                if depth < 0:
                    break
            elif c == ";" and depth == 0:
                break
            i += 1
        parts.append(source[m.end():i])
    return "\n".join(parts)


def _targets_api(argument: str, source: str) -> bool:
    """True when the request's address names `/api`, directly or through this file.

    A literal or template containing `/api` is direct. A bare name is read
    from its same-file `const|let|var` initializer, and a call from the body
    of the same-file function it names. An address that arrives as a
    parameter or from another file is not visible here.
    """
    arg = argument.strip()
    if "/api" in arg:
        return True
    if _IDENT.match(arg):
        return "/api" in _initializer(source, arg)
    call = _CALL.match(arg)
    if call:
        body = _function_body(source, call.group(1))
        return body is not None and "/api" in body
    return False


def _hand_built_sessions(ctx: RuleContext, text: str) -> list[int]:
    """Offsets where code names the session as a query parameter itself.

    Two forms: a `URLSearchParams` `.set('token', ...)` / `.append('token', ...)`
    outside comments and strings, and a `token=` written into an `apiFetch`
    address. Image and download links build theirs with the
    `URLSearchParams({ token })` constructor in a helper, which is not matched.
    """
    offsets = [
        m.start() for m in _SESSION_PARAM_SET.finditer(ctx.source)
        if text[m.start()] == "."
    ]
    for m in _API_FETCH.finditer(text):
        start, end = _argument(text, m.end() - 1, 0)
        if end and _SESSION_IN_ADDRESS.search(ctx.source[start:end]):
            offsets.append(m.start())
    return sorted(offsets)


def _finding(ctx: RuleContext, text: str, start: int, message: str, fix_hint: str) -> Finding:
    line = text.count("\n", 0, start) + 1
    col = start - (text.rfind("\n", 0, start) + 1) + 1
    snippet = ctx.lines[line - 1] if 0 < line <= len(ctx.lines) else ""
    return Finding(
        rule_id=_RULE_ID, severity="error", file=ctx.file_path, line=line, col=col,
        snippet=snippet, message=message, fix_hint=fix_hint,
    )


def _check(ctx: RuleContext) -> Iterable[Finding]:
    """Yield a finding for each `/api` request made without `apiFetch`, and each
    session query parameter built by hand.


    Comments and string contents are blanked to find the calls (offsets are
    preserved), and the address is then read from the original text. Flags
    `fetch(`, `new EventSource(` and, in a file that uses `XMLHttpRequest`,
    `.open(method, url)`, when the address targets `/api`. Files in
    `allow_files` (the shared request function itself) are skipped.
    """
    if ctx.is_python or _is_test_file(ctx.file_path):
        return
    if not ctx.file_path.endswith((".ts", ".svelte")):
        return
    settings = rule_settings(ctx.config, _RULE_ID)
    if ctx.file_path in (settings.get("allow_files") or []):
        return
    text = _blank(ctx.source)
    calls: list[tuple[str, int, str]] = []

    def collect(kind: str, match: re.Match, index: int) -> None:
        start, end = _argument(text, match.end() - 1, index)
        calls.append((kind, match.start(), ctx.source[start:end]))

    for m in _FETCH_CALL.finditer(text):
        collect("fetch", m, 0)
    for m in _EVENT_SOURCE.finditer(text):
        collect("EventSource", m, 0)
    if "XMLHttpRequest" in text:
        for m in _XHR_OPEN.finditer(text):
            collect("XMLHttpRequest", m, 1)
    for kind, start, argument in sorted(calls, key=lambda item: item[1]):
        if not _targets_api(argument, ctx.source):
            continue
        yield _finding(
            ctx, text, start,
            message=(
                f"`{kind}` reaches `/api` without the shared request function. "
                "Only that function signs every call the same way, with the "
                "signed-in session the devices listener needs for an `/api` "
                "request, so a call that skips it works on the main computer and "
                "fails from a phone or another computer."
            ),
            fix_hint=(
                "Call `apiFetch(url, init)` from `$lib/api/request`; it signs the "
                "call with the signed-in session. For a route that declares the "
                "session as a required query parameter, pass `sessionInAddress: "
                "true` and let `apiFetch` add it; never build that parameter "
                "yourself. For an `<img src>` or download link, which cannot set "
                "a header, build the URL in a helper like `artifactImage.ts`."
            ),
        )
    for start in _hand_built_sessions(ctx, text):
        yield _finding(
            ctx, text, start,
            message=(
                "The session is added to a request address by hand. `apiFetch` "
                "owns the session on every request, so a second copy built "
                "elsewhere is a second owner that can drift from the header."
            ),
            fix_hint=(
                "Drop the `token` parameter: `apiFetch` sends the session in the "
                "header. If the route declares the session as a required query "
                "parameter, pass `sessionInAddress: true` instead. An `<img src>` "
                "or download link builds its URL in a helper like `artifactImage.ts`."
            ),
        )


RULE = Rule(
    id=_RULE_ID,
    severity="error",
    description=(
        "Detects `fetch(`, `new EventSource(` and `XMLHttpRequest` `.open(` calls "
        "in `frontend/src/**/*.{ts,svelte}` (tests excluded) whose address "
        "targets `/api`, outside the shared request function "
        "`frontend/src/lib/api/request.ts`. The devices listener (the "
        "remote-apps entrance) admits an `/api` request only with the "
        "signed-in session, and refuses one whose header and address name "
        "different sessions; the shared function owns the session for every "
        "call, so a raw call works on the main computer and can fail from a "
        "phone or another computer."
    ),
    false_positive_examples=[
        "`fetch(src)` where `src` is a function parameter (`imageFailure.ts`): "
        "the address is not visible in the file, so it is not flagged.",
        "A request to another origin: only an address containing `/api` is "
        "judged.",
        "The shared request function itself (`request.ts`) calls `fetch`; it is "
        "in `allow_files`.",
    ],
    fix_hint=(
        "Call `apiFetch` from `$lib/api/request` (`sessionInAddress: true` for "
        "a route that requires the session in the address); keep an `<img "
        "src>` or download link's session in its URL query, built by a helper."
    ),
    check=_check,
    calibration_note=(
        "Residual, known: an address built in another file and passed in as a "
        "parameter, or `const f = fetch` aliasing, is not seen. A hand-built "
        "session is seen only as `.set|append('token', ...)` or `token=` inside "
        "an `apiFetch` address; a `URLSearchParams({ token })` constructor is "
        "left alone because image and download link helpers need it."
    ),
)
