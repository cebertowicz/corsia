"""Rule: missing-public-docstring.

Public functions and methods (no leading underscore) must carry a docstring
explaining what they do. This is the engineering-standards.md mandate:
    "All public functions should have docstrings explaining what they do."

What this rule fires on:
    - `def foo(...)` at module level with no docstring (no leading
      underscore in name).
    - `def foo(...)` at class level (a method) with no docstring (no
      leading underscore in name).
    - Async variants (`async def foo`) follow the same rules.

What this rule does NOT fire on:
    - Names starting with `_` — private by convention; out of scope.
    - Dunder methods (`__init__`, `__call__`, etc.) — generally have
      well-known semantics; flagging dunder absence creates noise. (Most
      dunders' contracts are language-defined; explicit docstrings on
      every `__init__` is cargo-culting. We require docstrings on dunders
      that are NOT well-known — but the simplest sprint-1 rule is to
      skip all dunders. Promote at calibration if signal warrants.)
    - `@property` setters/deleters when the @property getter has a
      docstring on the same name (the property carries the contract).
    - Test functions in test paths (per `test_path_globs`).

Scope:
    backend/**/*.py + tools/**/*.py via include_globs in default config.
    Frontend/TypeScript is out of scope this sprint per slice prompt.

Severity: warning during calibration; promote to error after paydown.
"""

from __future__ import annotations

import ast
from typing import Iterable

from ..findings import Finding
from ._base import Rule, RuleContext


_RULE_ID = "missing-public-docstring"


def _is_dunder(name: str) -> bool:
    """`__init__`, `__call__`, etc. — language-defined names."""
    return name.startswith("__") and name.endswith("__")


def _is_private(name: str) -> bool:
    """Single-leading-underscore = private by convention."""
    return name.startswith("_") and not _is_dunder(name)


def _has_docstring(node: ast.FunctionDef | ast.AsyncFunctionDef) -> bool:
    """True if the first body statement is a string Constant (docstring)."""
    body = getattr(node, "body", [])
    if not body:
        return False
    first = body[0]
    if isinstance(first, ast.Expr) and isinstance(first.value, ast.Constant):
        return isinstance(first.value.value, str)
    return False


def _is_property_setter_or_deleter(node: ast.FunctionDef | ast.AsyncFunctionDef) -> bool:
    """True if this function is a `@<name>.setter` or `@<name>.deleter`.

    Property setters often share their contract with the property's getter
    docstring; we don't double-flag.
    """
    for deco in node.decorator_list:
        if isinstance(deco, ast.Attribute) and deco.attr in {"setter", "deleter"}:
            return True
    return False


def _check(ctx: RuleContext) -> Iterable[Finding]:
    """Walk the AST; yield findings for missing public docstrings.

    Note: the engine applies `include_globs` and `exclude_globs` from
    config BEFORE invoking this check. The rule itself only short-circuits
    on non-Python files and on test paths.
    """
    if not ctx.is_python or ctx.ast_tree is None:
        return
    if ctx.is_test_path:
        return
    for node in ast.walk(ctx.ast_tree):
        if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            continue
        if _is_private(node.name):
            continue
        if _is_dunder(node.name):
            continue
        if _is_property_setter_or_deleter(node):
            continue
        if _has_docstring(node):
            continue
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
                f"public function `{node.name}` has no docstring. "
                "engineering-standards.md: all public functions must "
                "have a docstring explaining what they do."
            ),
            fix_hint=(
                'Add a docstring: """One-line summary."""  '
                "(extend with Args/Returns/Raises if non-trivial)."
            ),
        )


RULE = Rule(
    id=_RULE_ID,
    severity="warning",
    description=(
        "Public functions and methods (no leading underscore) must carry "
        "a docstring per engineering-standards.md. Dunders, properties' "
        "setters/deleters, and test paths are exempt."
    ),
    false_positive_examples=[
        "Private functions (leading underscore) — exempt.",
        "Dunder methods (`__init__`, etc.) — exempt for sprint 2.",
        "Test paths — exempt.",
    ],
    fix_hint=(
        "Add a one-line docstring describing what the function does. "
        "Extend with Args/Returns/Raises if the signature is non-trivial."
    ),
    check=_check,
)
