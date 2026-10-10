"""Detect TODO/FIXME/XXX comments without tracking markers."""

from __future__ import annotations

import io
import re
import tokenize
from typing import Iterable

from ..config import rule_settings
from ..findings import Finding
from ._base import Rule, RuleContext


_RULE_ID = "untracked-todo"

_TOKEN_RE = re.compile(r"\b(TODO|FIXME|XXX)\b", re.IGNORECASE)


def _iter_comments(source: str):
    """Yield tokenized Python comments."""
    try:
        tokens = tokenize.generate_tokens(io.StringIO(source).readline)
        for tok in tokens:
            if tok.type == tokenize.COMMENT:
                yield tok.start[0], tok.start[1] + 1, tok.string
    except (tokenize.TokenizeError, IndentationError):
        return


def _check(ctx: RuleContext) -> Iterable[Finding]:
    """Scan tokenized comments; yield findings for untracked TODOs."""
    if not ctx.is_python:
        return
    settings = rule_settings(ctx.config, _RULE_ID)
    raw_markers = settings.get("tracking_marker_patterns", []) or []
    marker_patterns = [re.compile(p) for p in raw_markers]
    for line_no, col, comment_text in _iter_comments(ctx.source):
        match = _TOKEN_RE.search(comment_text)
        if not match:
            continue
        if any(p.search(comment_text) for p in marker_patterns):
            continue
        snippet = ctx.lines[line_no - 1] if 0 < line_no <= len(ctx.lines) else comment_text
        yield Finding(
            rule_id=_RULE_ID,
            severity="warning",
            file=ctx.file_path,
            line=line_no,
            col=col,
            snippet=snippet,
            message=(
                f"{match.group(1).upper()} comment has no tracking marker."
                " Add a #issue-id, audit-run/session-NN-..., "
                "task-..., or `(deferred: <reason>)` annotation."
            ),
            fix_hint=(
                "Append a tracking marker, e.g. `# TODO(#123): ...`, "
                "`# FIXME task-review-lifecycle: ...`, or `# TODO (deferred: needs "
                "owner sign-off): ...`."
            ),
        )


RULE = Rule(
    id=_RULE_ID,
    severity="warning",
    description=(
        "Detects TODO / FIXME / XXX comments that lack a tracking marker "
        "(issue ref, audit-run ref, task ID, or explicit "
        "(deferred: ...) note)."
    ),
    false_positive_examples=[
        "`# TODO(#42): refactor this loop` — has `#42` issue ref.",
        "`# FIXME task-review-lifecycle: once gate fires, remove` — has task ref.",
        "`# TODO (deferred: blocked on Steve approval)` — explicit deferral.",
    ],
    fix_hint=(
        "Append a tracking marker so the TODO doesn't go stale: a github "
        "issue (`#NNN`), an audit-run reference, a "
        "CORSIA task id, or a `(deferred: <reason>)` annotation."
    ),
    check=_check,
)
