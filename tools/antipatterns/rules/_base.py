"""Rule base class for the anti-pattern detector.

A rule is a Protocol-shaped dataclass: stable id, default severity, descriptive
fields (description, false-positive examples, recommended fix), and a single
`check(file_path, source, ast_tree) -> Iterable[Finding]` method.

Why a dataclass + callable rather than an abstract base class: rules tend to
need shared metadata (id, severity, description, calibration record) more than
shared behavior. Each rule's check logic is its own function. Composition >
inheritance; one Rule instance per rule module.
"""

from __future__ import annotations

import ast
from dataclasses import dataclass, field
from typing import Callable, Iterable

from ..findings import Finding


# Each rule's check function takes a parsed view of the file and yields findings.
# The engine handles file IO + parsing; rules consume what they need.
CheckFn = Callable[["RuleContext"], Iterable[Finding]]


@dataclass
class RuleContext:
    """The per-file context the engine hands to each rule's check function.

    Fields:
        file_path: Repo-relative path of the file being analyzed.
        source: Raw source text of the file (utf-8 decoded).
        lines: source.splitlines(keepends=False) — pre-split for line-based
            rules so they don't each re-split.
        ast_tree: Parsed AST, or None if the file failed to parse (some rules
            are line-based and don't need the AST; others gate on it).
        is_python: True iff file ends in `.py` (rules can early-out otherwise).
        is_test_path: True iff file matches a test-path glob (used by rules
            like `mock-where-real-needed` that only fire in non-test code).
        config: The merged config (default + per-repo). Rules read their
            section under `config.rules[rule_id]` for tuning.
    """

    file_path: str
    source: str
    lines: list[str]
    ast_tree: ast.AST | None
    is_python: bool
    is_test_path: bool
    config: dict


@dataclass
class Rule:
    """A single anti-pattern rule.

    Fields are metadata + the check callable. The engine uses metadata to
    filter (enabled? severity threshold?) and renders the description in
    human output. The check callable does the real work.
    """

    id: str
    severity: str  # default; can be overridden via per-repo config.
    description: str
    false_positive_examples: list[str]
    fix_hint: str
    check: CheckFn = field(repr=False)
    # Calibration record — populated post-corpus-run. Not load-bearing for
    # detection; surfaced in `--describe` output for human authors.
    calibration_note: str = ""
