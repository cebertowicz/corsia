"""Detect frontend timers that re-read server state on a recurring timer."""

from __future__ import annotations

import re
from typing import Iterable

from ..config import rule_settings
from ..findings import Finding
from ._base import Rule, RuleContext


_RULE_ID = "periodic-server-reread"

_TIMER_CALL = re.compile(r"\b(?:window\.|globalThis\.)?(setInterval|setTimeout)\s*\(")
_BARE_CALL = re.compile(r"(?<![\w$.])([A-Za-z_$][\w$]*)\s*\(")
_MEMBER_CALL = re.compile(r"\.\s*([A-Za-z_$][\w$]*)\s*\(")
_IDENT_REF = re.compile(r"^[A-Za-z_$][\w$]*(?:\.[A-Za-z_$][\w$]*)*$")
_FETCH_CALL = re.compile(r"(?<![\w$])fetch\s*\(")
_SET_TIMEOUT = re.compile(r"\bsetTimeout\s*\(")

# `fetch(` itself is matched separately. These names are the frontend's own
# server reads: the bare or capitalised, lowercase-leading load/refresh/reload/poll/fetch/sync
# verbs (`refresh()`, `store.reload()`, `loadThreads()`), and the `read*`
# helpers exported by `frontend/src/lib/api/`.
_DEFAULT_READ_NAME = (
    r"^((load|refresh|reload|poll|fetch|sync)([A-Z]\w*)?$"
    r"|read(Plan|Activity|Operator|ThreadPlanStates|UpdateCards))"
)

_KEYWORDS = frozenset({
    "if", "for", "while", "switch", "catch", "function", "return", "typeof",
    "await", "async", "void", "new", "else", "do", "try",
})


def _blank(source: str) -> str:
    """Return `source` with comments and string contents replaced by spaces.

    Offsets and newlines are preserved so match positions map back to the
    original lines. Template-literal `${...}` expressions are blanked too.
    """
    out: list[str] = []
    i, n = 0, len(source)
    while i < n:
        c = source[i]
        two = source[i:i + 2]
        if two == "//":
            j = source.find("\n", i)
            j = n if j == -1 else j
            out.append(" " * (j - i))
            i = j
        elif two == "/*":
            j = source.find("*/", i + 2)
            j = n if j == -1 else j + 2
            out.append("".join(ch if ch == "\n" else " " for ch in source[i:j]))
            i = j
        elif c in "'\"`":
            j = i + 1
            while j < n and source[j] != c:
                if source[j] == "\\":
                    j += 1
                elif c != "`" and source[j] == "\n":
                    break
                j += 1
            j = min(j, n)
            closed = j < n and source[j] == c
            out.append(c + "".join(
                ch if ch == "\n" else " " for ch in source[i + 1:j]
            ) + (c if closed else ""))
            i = j + 1 if closed else j
        else:
            out.append(c)
            i += 1
    return "".join(out)


def _balanced(text: str, open_idx: int) -> int:
    """Index just past the bracket that closes `text[open_idx]`, or -1."""
    pairs = {"(": ")", "[": "]", "{": "}"}
    stack: list[str] = []
    for i in range(open_idx, len(text)):
        c = text[i]
        if c in pairs:
            stack.append(pairs[c])
        elif stack and c == stack[-1]:
            stack.pop()
            if not stack:
                return i + 1
    return -1


def _first_argument(text: str, open_paren: int) -> str:
    """Text of the first top-level argument of the call opened at `open_paren`."""
    end = _balanced(text, open_paren)
    if end == -1:
        return ""
    depth = 0
    for i in range(open_paren + 1, end - 1):
        c = text[i]
        if c in "([{":
            depth += 1
        elif c in ")]}":
            depth -= 1
        elif c == "," and depth == 0:
            return text[open_paren + 1:i]
    return text[open_paren + 1:end - 1]


def _function_body(text: str, name: str) -> str | None:
    """Body of a function named `name` declared in this file, or None.

    Handles `function name(...) {...}` and `const name = (...) => ...`.
    """
    esc = re.escape(name)
    decl = re.search(rf"\bfunction\s*\*?\s*{esc}\s*(?:<[^>(]*>)?\s*\(", text)
    if decl:
        params_end = _balanced(text, decl.end() - 1)
        brace = text.find("{", params_end) if params_end != -1 else -1
        if brace != -1:
            end = _balanced(text, brace)
            return text[brace:end] if end != -1 else None
    arrow = re.search(
        rf"\b(?:const|let|var)\s+{esc}\s*(?::[^=]+)?=\s*(?:async\s*)?", text
    )
    if arrow:
        i = arrow.end()
        if text[i:i + 1] == "(":
            i = _balanced(text, i)
        else:
            m = re.match(r"[A-Za-z_$][\w$]*", text[i:])
            i = i + m.end() if m else -1
        arrow_at = text.find("=>", i) if i != -1 else -1
        if arrow_at != -1:
            j = arrow_at + 2
            while j < len(text) and text[j] in " \t\n":
                j += 1
            if text[j:j + 1] == "{":
                end = _balanced(text, j)
                return text[j:end] if end != -1 else None
            eol = text.find("\n", j)
            return text[j:eol if eol != -1 else len(text)]
    return None


def _called_names(text: str) -> tuple[set[str], set[str]]:
    """Return the (bare, member) names called in `text`."""
    bare = {m.group(1) for m in _BARE_CALL.finditer(text)} - _KEYWORDS
    member = {m.group(1) for m in _MEMBER_CALL.finditer(text)}
    return bare, member


def _expand(text: str, callback: str) -> tuple[list[str], set[str]]:
    """Resolve the callback through same-file functions, however deep.

    Returns the code texts reached (the callback, then each resolved body)
    and the names that get the name test. A bare name that resolves to a
    same-file function is judged by its body, never by its name; a visited
    set ends cycles. A member call (`x.refresh()`) is never resolved against
    same-file functions: it always gets the name test.
    """
    texts = [callback]
    unresolved: set[str] = set()
    visited: set[str] = set()

    def visit(code: str, extra_bare: set[str], extra_member: set[str]) -> None:
        """Resolve each name `code` calls, recursing into same-file bodies."""
        bare, member = _called_names(code)
        unresolved.update(member | extra_member)
        for name in sorted(bare | extra_bare):
            if name in visited:
                continue
            body = _function_body(text, name)
            if body is None:
                unresolved.add(name)
            else:
                visited.add(name)
                texts.append(body)
                visit(body, set(), set())

    ref = callback.strip()
    if _IDENT_REF.match(ref) and "." in ref:
        visit(callback, set(), {ref.rsplit(".", 1)[-1]})
    elif _IDENT_REF.match(ref):
        visit(callback, {ref}, set())
    else:
        visit(callback, set(), set())
    return texts, unresolved


def _is_test_file(path: str) -> bool:
    return (
        path.endswith((".test.ts", ".spec.ts"))
        or "/__tests__/" in f"/{path}"
    )


def _check(ctx: RuleContext) -> Iterable[Finding]:
    """Yield a finding for each timer whose callback re-reads server state.

    Heuristic, deliberately simple: find every `setInterval(...)` and
    `setTimeout(...)` call (comments and string contents blanked first), take
    its first argument as the callback, and resolve it through same-file
    functions (function declarations and `const f = ... =>`) with a visited
    set and no depth limit. A bare name that resolves is judged by its body;
    a name that does not resolve (an import) and every member call such as
    `store.refresh()` gets the name test. The timer is flagged when the reached code
    contains `fetch(`, or an unresolved call to an identifier matching
    `server_read_pattern` (default: the bare or capitalised, lowercase-leading
    load/refresh/reload/poll/fetch/sync verbs and the `read*` helpers in
    `frontend/src/lib/api/`). A `setTimeout` also needs to be
    self-rescheduling: the reached code must contain another `setTimeout(`.
    That is a poll whatever the delay, retry-with-backoff included. A
    one-shot timer is a deadline or debounce, not a poll. Files listed in
    `allow_files` (reviewed accepted false positives) are skipped.
    """
    if ctx.is_python or _is_test_file(ctx.file_path):
        return
    if not ctx.file_path.endswith((".ts", ".svelte")):
        return
    settings = rule_settings(ctx.config, _RULE_ID)
    if ctx.file_path in (settings.get("allow_files") or []):
        return
    pattern = settings.get("server_read_pattern") or _DEFAULT_READ_NAME
    read_name = re.compile(pattern)
    text = _blank(ctx.source)
    for match in _TIMER_CALL.finditer(text):
        kind = match.group(1)
        open_paren = match.end() - 1
        callback = _first_argument(text, open_paren)
        if not callback.strip():
            continue
        texts, unresolved = _expand(text, callback)
        if kind == "setTimeout" and not any(_SET_TIMEOUT.search(t) for t in texts):
            continue
        reads = any(_FETCH_CALL.search(t) for t in texts) or any(
            read_name.match(n) for n in unresolved
        )
        if not reads:
            continue
        line = text.count("\n", 0, match.start()) + 1
        col = match.start() - (text.rfind("\n", 0, match.start()) + 1) + 1
        snippet = ctx.lines[line - 1] if 0 < line <= len(ctx.lines) else ""
        yield Finding(
            rule_id=_RULE_ID,
            severity="error",
            file=ctx.file_path,
            line=line,
            col=col,
            snippet=snippet,
            message=(
                f"`{kind}` re-reads server state on a recurring timer. A timer "
                "poll hides a producer that never emits an event and gets "
                "copied from file to file."
            ),
            fix_hint=(
                "Re-read on the event that changes the state: have the "
                "backend producer emit a websocket nudge and refresh from "
                "that handler (and once on reconnect). For a time-driven "
                "change, arm one `setTimeout` at the known deadline instead "
                "of an interval."
            ),
        )


RULE = Rule(
    id=_RULE_ID,
    severity="error",
    description=(
        "Detects `setInterval` and self-rescheduling `setTimeout` timers in "
        "`frontend/src/**/*.{ts,svelte}` (tests excluded) whose callback "
        "re-reads server state: `fetch(`, an API helper, or an unresolved "
        "call to a load/refresh/reload/poll/fetch/sync verb, inline or "
        "reached through same-file functions. Server state changes are pushed as events; a "
        "client timer that re-reads on a recurring timer, retries with backoff included, copies itself between "
        "components and covers for producers that never emit."
    ),
    false_positive_examples=[
        "Pure local clocks: a `setInterval` that only updates `Date.now()`, "
        "an elapsed-seconds counter, or a typewriter tick.",
        "A one-shot `setTimeout` that re-reads once at a known deadline "
        "(debounce, overdue time, lease expiry); only a callback that re-arms "
        "itself counts as a poll.",
        "A single refresh at lease expiry with backoff "
        "(`HostAccessLeaseControl.svelte`): the interval is a local clock "
        "and the re-read fires only once a deadline passes. The rule cannot "
        "tell that apart from a poll, so the file is in `allow_files` with "
        "that reason.",
        "A bounded retry after a failed request (a self-rearming "
        "`setTimeout` that re-reads): a poll as far as the rule can tell. "
        "Remedy: a reviewed `allow_files` entry with a stated reason.",
        "A local helper with a verb name, e.g. `refreshRelativeTimes()`, "
        "`syncScrollPosition()`, `loadFromCache()`: handled. A name that "
        "resolves to a same-file function is judged by its body, so a "
        "local-only body is not flagged.",
        "Residual, known: a member call is never resolved against same-file "
        "functions, so a verb-named method with a local-only body (class "
        "method, object-literal method, `x.refresh()` even when the file "
        "also defines a local `refresh`), e.g. "
        "`class Store { refresh() { this.t = Date.now(); } }` run from a "
        "timer as `store.refresh()`, is always name-tested and IS flagged. "
        "Remedy: rename the helper, or a reviewed `allow_files` entry with "
        "a stated reason.",
    ],
    fix_hint=(
        "Emit a websocket event from the backend producer and refresh from "
        "its handler (and on reconnect). For a time-driven change, arm one "
        "`setTimeout` at the deadline rather than a recurring timer."
    ),
    check=_check,
    calibration_note=(
        "Calibrated on the plan-state polls removed in cebertowicz/corsia#155 "
        "(flagged) and the frontend's local clocks (not flagged). Known "
        "remaining pollers are excluded per file in default-config.toml, "
        "each citing its issue; the list may only shrink."
    ),
)
