"""Rule: silent-error-swallow.

Detects exception handlers that hide the failure rather than handling it.
Patterns this rule catches:

    1. `except: pass`             — bare except, no handling.
    2. `except Exception: pass`   — catch-all, swallowed.
    3. `except: <only logger.debug(...)>` — logged at debug, silent in prod.
    4. `except Exception as e: return None`/`return ""`/`return []`/`return False`
       — handler hides the failure with a benign-looking value.

Acceptable patterns (not flagged):
    - `except Exception: logger.exception(...)` followed by a `raise`.
    - Cleanup paths inside `finally:` are out of scope — `finally` blocks
      can swallow legitimately during teardown.
    - `except SpecificException as e: <recovery code>` — explicit narrow
      handling is the correct shape.

Severity: error by spec; calibration may downgrade to warning.
"""

from __future__ import annotations

import ast
from typing import Iterable

from ..findings import Finding
from ._base import Rule, RuleContext


_RULE_ID = "silent-error-swallow"


def _handler_only_passes(handler: ast.ExceptHandler) -> bool:
    """True if the only statement in this handler is `pass`."""
    return len(handler.body) == 1 and isinstance(handler.body[0], ast.Pass)


def _handler_only_debug_logs(handler: ast.ExceptHandler) -> bool:
    """True if every statement in the handler is a `*.debug(...)` call.

    Catches `logger.debug(...)`, `log.debug(...)`, `logging.debug(...)`.
    `logger.exception(...)` and `logger.error(...)` are real handling and
    don't fire. A handler that *also* re-raises is fine; if the handler
    has only debug calls and no re-raise, that's the silent pattern.
    """
    if not handler.body:
        return False
    saw_debug = False
    for stmt in handler.body:
        if not isinstance(stmt, ast.Expr):
            return False
        call = stmt.value
        if not isinstance(call, ast.Call):
            return False
        func = call.func
        if not isinstance(func, ast.Attribute):
            return False
        if func.attr != "debug":
            return False
        saw_debug = True
    return saw_debug


def _handler_returns_benign(handler: ast.ExceptHandler) -> bool:
    """True if the handler's only effect is `return` of a benign literal.

    Benign = None, "", [], {}, False, 0. These hide a failure from callers
    that wouldn't notice the absence.
    """
    if len(handler.body) != 1:
        return False
    stmt = handler.body[0]
    if not isinstance(stmt, ast.Return):
        return False
    val = stmt.value
    if val is None:
        return True
    if isinstance(val, ast.Constant) and val.value in (None, "", 0, False):
        return True
    if isinstance(val, ast.List) and not val.elts:
        return True
    if isinstance(val, ast.Dict) and not val.keys:
        return True
    if isinstance(val, ast.Tuple) and not val.elts:
        return True
    return False


def _is_finally_descendant(handler: ast.ExceptHandler, tree: ast.AST) -> bool:
    """True if the enclosing Try is itself in a `finally` body.

    Implementation note: `ast` doesn't store parent links by default, so
    we walk the tree once and check ancestor membership. We can short-
    circuit: handler's parent chain crosses a `Try.finalbody`.
    """
    target = handler
    parents: dict[int, ast.AST] = {}
    for parent in ast.walk(tree):
        for child in ast.iter_child_nodes(parent):
            parents[id(child)] = parent
    cur: ast.AST | None = target
    while cur is not None:
        parent = parents.get(id(cur))
        if isinstance(parent, ast.Try) and any(
            cur is fbn or cur in ast.walk(fbn) for fbn in parent.finalbody
        ):
            return True
        cur = parent
    return False


def _is_bare_except(handler: ast.ExceptHandler) -> bool:
    """`except:` (no exception type) — the bare catch-all."""
    return handler.type is None


def _catches_exception_class(handler: ast.ExceptHandler) -> bool:
    """`except Exception:` or `except (Exception, ...)`."""
    if handler.type is None:
        return False
    if isinstance(handler.type, ast.Name) and handler.type.id == "Exception":
        return True
    if isinstance(handler.type, ast.Attribute) and handler.type.attr == "Exception":
        return True
    if isinstance(handler.type, ast.Tuple):
        for elt in handler.type.elts:
            if isinstance(elt, ast.Name) and elt.id == "Exception":
                return True
            if isinstance(elt, ast.Attribute) and elt.attr == "Exception":
                return True
    return False


def _check(ctx: RuleContext) -> Iterable[Finding]:
    """Walk the AST; yield findings for silent-swallow patterns."""
    if not ctx.is_python or ctx.ast_tree is None:
        return
    tree = ctx.ast_tree
    for node in ast.walk(tree):
        if not isinstance(node, ast.Try):
            continue
        for handler in node.handlers:
            # Only catch-all-ish handlers can swallow silently. A narrow
            # `except KeyError: ...` is the right shape; trust it.
            if not (_is_bare_except(handler) or _catches_exception_class(handler)):
                continue
            # Skip if inside `finally` — teardown swallowing is allowed.
            if _is_finally_descendant(handler, tree):
                continue
            reason = None
            if _handler_only_passes(handler):
                reason = "handler is `pass` — failure is fully silent."
            elif _handler_only_debug_logs(handler):
                reason = (
                    "handler logs at `.debug(...)` only — silent in prod "
                    "where debug is filtered out."
                )
            elif _handler_returns_benign(handler):
                reason = (
                    "handler returns a benign value (None / '' / 0 / [] / "
                    "False) — caller can't tell the call failed."
                )
            if not reason:
                continue
            line = handler.lineno
            col = handler.col_offset + 1
            snippet = ctx.lines[line - 1] if 0 < line <= len(ctx.lines) else ""
            yield Finding(
                rule_id=_RULE_ID,
                severity="warning",  # engine overlays the per-config severity
                file=ctx.file_path,
                line=line,
                col=col,
                snippet=snippet,
                message=f"silent error swallow: {reason}",
                fix_hint=(
                    "Either re-raise after logging, narrow the except to a "
                    "specific exception type, or recover with explicit "
                    "rationale (logger.exception + raise; or `except "
                    "ValueError: <handle>`)."
                ),
            )


RULE = Rule(
    id=_RULE_ID,
    severity="warning",
    description=(
        "Detects exception handlers that swallow failures: bare `except`, "
        "`except Exception: pass`, debug-only logging, or return of a "
        "benign value that hides the error from callers."
    ),
    false_positive_examples=[
        "`except Exception: logger.exception(...)` followed by `raise` — re-raised.",
        "Cleanup paths inside a `finally:` block — teardown can swallow.",
        "Narrow `except KeyError as e:` with explicit recovery — not flagged.",
    ],
    fix_hint=(
        "Either re-raise after logging or narrow the except to a specific "
        "exception type with explicit recovery."
    ),
    check=_check,
)
