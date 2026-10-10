"""Rule: mock-where-real-needed.

Detects use of `unittest.mock` (MagicMock / AsyncMock / patch) inside
production paths. Mocks belong in tests; production code that imports
mock is either a debugging shim left behind or a fake-real-system
shortcut that bypasses real integrations.

Test paths are excluded via `global.test_path_globs`. The rule fires on:
    - `import unittest.mock`
    - `from unittest.mock import ...`
    - `from unittest import mock`
    - References to `MagicMock`, `AsyncMock`, `Mock`, `patch` as
      bare names (likely imported elsewhere).

We narrow to the structural form (mock-in-non-test-path) here; the
semantic form (mock-substituting-for-real-system-in-an-integration-test)
is a `code-reviewer` PR-time concern.

Severity: error per spec; calibration may downgrade.
"""

from __future__ import annotations

import ast
from typing import Iterable

from ..findings import Finding
from ._base import Rule, RuleContext


_RULE_ID = "mock-where-real-needed"


_MOCK_MODULE_PATHS = {"unittest.mock", "mock"}
_MOCK_NAMES = {"MagicMock", "AsyncMock", "Mock", "patch", "PropertyMock", "create_autospec"}


def _check(ctx: RuleContext) -> Iterable[Finding]:
    """Yield findings for mock imports / usage inside production paths."""
    if not ctx.is_python or ctx.ast_tree is None:
        return
    if ctx.is_test_path:
        return
    for node in ast.walk(ctx.ast_tree):
        line = getattr(node, "lineno", None)
        col = getattr(node, "col_offset", 0)
        if line is None:
            continue
        snippet = ctx.lines[line - 1] if 0 < line <= len(ctx.lines) else ""
        col_one_indexed = (col or 0) + 1
        # `import unittest.mock` / `import mock`
        if isinstance(node, ast.Import):
            for alias in node.names:
                if alias.name in _MOCK_MODULE_PATHS:
                    yield Finding(
                        rule_id=_RULE_ID,
                        severity="warning",
                        file=ctx.file_path,
                        line=line,
                        col=col_one_indexed,
                        snippet=snippet,
                        message=(
                            f"production path imports `{alias.name}`. "
                            "Mocks belong in test paths only."
                        ),
                        fix_hint=(
                            "Move the import to a test file under "
                            "`tests/` / `test_*.py` / `conftest.py`, or "
                            "replace the mock with a real injection."
                        ),
                    )
        # `from unittest.mock import ...` / `from unittest import mock`
        elif isinstance(node, ast.ImportFrom):
            module_name = node.module or ""
            if module_name in _MOCK_MODULE_PATHS:
                yield Finding(
                    rule_id=_RULE_ID,
                    severity="warning",
                    file=ctx.file_path,
                    line=line,
                    col=col_one_indexed,
                    snippet=snippet,
                    message=(
                        f"production path imports from `{module_name}`. "
                        "Mocks belong in test paths only."
                    ),
                    fix_hint=(
                        "Move the import to a test file, or replace the "
                        "mock with a real injection / fixture."
                    ),
                )
            elif module_name == "unittest" and any(a.name == "mock" for a in node.names):
                yield Finding(
                    rule_id=_RULE_ID,
                    severity="warning",
                    file=ctx.file_path,
                    line=line,
                    col=col_one_indexed,
                    snippet=snippet,
                    message=(
                        "production path imports `mock` from `unittest`. "
                        "Mocks belong in test paths only."
                    ),
                    fix_hint=(
                        "Move the import to a test file, or replace the "
                        "mock with a real injection / fixture."
                    ),
                )


RULE = Rule(
    id=_RULE_ID,
    severity="warning",
    description=(
        "Detects `unittest.mock` imports inside production (non-test) "
        "paths. Test path classification is via `global.test_path_globs`."
    ),
    false_positive_examples=[
        "Imports inside `**/tests/**`, `**/test_*.py`, `**/conftest.py`.",
        "A fixture file in a clearly-named fixtures subdir (`fixtures/`).",
    ],
    fix_hint=(
        "Move mock imports to test paths. Production code should not "
        "depend on `unittest.mock`; substitute real injections."
    ),
    check=_check,
)
