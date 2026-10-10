"""Rule: fallback-chain-masks-error."""

from __future__ import annotations

import ast
from typing import Iterable

from ..findings import Finding
from ._base import Rule, RuleContext


_RULE_ID = "fallback-chain-masks-error"


def _is_get_or_subscript(expr: ast.expr) -> bool:
    """True if `expr` is `x.get(...)` or `x[k]`."""
    if isinstance(expr, ast.Call):
        func = expr.func
        if isinstance(func, ast.Attribute) and func.attr == "get":
            if len(expr.args) == 1 and not expr.keywords:
                return True
    if isinstance(expr, ast.Subscript):
        return True
    return False


def _check_or_chain(node: ast.BoolOp) -> bool:
    """True if this BoolOp is the chained-`.get()`/`[]` masking pattern."""
    if not isinstance(node.op, ast.Or):
        return False
    values = node.values
    if len(values) < 3:
        return False
    chained_count = sum(1 for v in values[:-1] if _is_get_or_subscript(v))
    return chained_count >= 2


def _try_returns_fallback_data(node: ast.Try) -> bool:
    """True if `node` is `try: ... except ...: <return data fallback>`."""
    for handler in node.handlers:
        if len(handler.body) != 1:
            continue
        stmt = handler.body[0]
        if not isinstance(stmt, ast.Return):
            continue
        val = stmt.value
        if val is None:
            continue
        if isinstance(val, ast.Constant):
            if val.value in (None, "", 0, False):
                continue
        if isinstance(val, ast.List) and not val.elts:
            continue
        if isinstance(val, ast.Dict) and not val.keys:
            continue
        return True
    return False


def _check(ctx: RuleContext) -> Iterable[Finding]:
    """Yield findings for fallback chain patterns."""
    if not ctx.is_python or ctx.ast_tree is None:
        return
    for node in ast.walk(ctx.ast_tree):
        if isinstance(node, ast.BoolOp) and _check_or_chain(node):
            line = node.lineno
            col = (node.col_offset or 0) + 1
            snippet = ctx.lines[line - 1] if 0 < line <= len(ctx.lines) else ""
            yield Finding(
                rule_id=_RULE_ID,
                severity="warning",
                file=ctx.file_path,
                line=line,
                col=col,
                snippet=snippet,
                message=(
                    "chained `.get(...) or .get(...) or default` masks "
                    "legitimate falsy values from intermediate lookups."
                ),
                fix_hint=(
                    "Use `dict.get(key, default)` per call (explicit "
                    "default per lookup), or check `key in dict` "
                    "explicitly so falsy values aren't treated as "
                    "missing."
                ),
            )
        elif isinstance(node, ast.Try) and _try_returns_fallback_data(node):
            line = node.lineno
            col = (node.col_offset or 0) + 1
            snippet = ctx.lines[line - 1] if 0 < line <= len(ctx.lines) else ""
            yield Finding(
                rule_id=_RULE_ID,
                severity="warning",
                file=ctx.file_path,
                line=line,
                col=col,
                snippet=snippet,
                message=(
                    "try/except returns a non-empty fallback value rather "
                    "than re-raising — caller can't distinguish 'real "
                    "result' from 'fallback because of error'."
                ),
                fix_hint=(
                    "Either re-raise after logging, narrow the except to "
                    "the specific failure mode, or return an explicit "
                    "Result/Optional type so callers see the failure."
                ),
            )


RULE = Rule(
    id=_RULE_ID,
    severity="warning",
    description=(
        "Detects chained `.get() or .get() or default` patterns that mask "
        "legitimate falsy values, and try/except blocks that substitute a "
        "real-looking fallback for the actual failure."
    ),
    false_positive_examples=[
        "`getattr(obj, attr, default)` — explicit single default; not chained.",
        "`x.get(key, default)` — explicit per-call default; not chained.",
        "`value or fallback` (single `or`) — standard idiom, not flagged.",
    ],
    fix_hint=(
        "Use explicit per-call defaults; don't chain `.get()`s with `or`. "
        "For try/except, re-raise or surface the failure as a typed result."
    ),
    check=_check,
)
