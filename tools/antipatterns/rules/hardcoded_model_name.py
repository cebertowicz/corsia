"""Detect hardcoded model-name literals in production runtime code."""

from __future__ import annotations

import ast
import fnmatch
import re
from typing import Iterable

from ..config import rule_settings
from ..findings import Finding
from ._base import Rule, RuleContext


_RULE_ID = "hardcoded-model-name"

_DEFAULT_PROVIDERS = (
    "anthropic", "openai", "hosted_vllm", "lmstudio", "openrouter",
    "together", "together_ai", "fireworks", "fireworks_ai", "replicate",
    "groq", "perplexity", "deepseek", "mistral", "cohere", "xai",
    "oMLX", "omlx",
)
_PROVIDER_ALT = "|".join(re.escape(p) for p in _DEFAULT_PROVIDERS)
_DEFAULT_PATTERN = re.compile(
    r"^(" + _PROVIDER_ALT + r")/[A-Za-z0-9][A-Za-z0-9._\-]+$"
)
_EMBEDDED_DDL_DEFAULT_PATTERN = re.compile(
    r"""\bDEFAULT\s+['"]((?:""" + _PROVIDER_ALT + r")/[A-Za-z0-9][A-Za-z0-9._\-]+)['\"]",
    re.IGNORECASE,
)


def _is_data_file(rel_path: str, data_globs: list[str]) -> bool:
    """True if this file is in the catalog/pricing/registry allow-list."""
    basename = rel_path.rsplit("/", 1)[-1]
    return any(
        fnmatch.fnmatch(rel_path, g) or fnmatch.fnmatch(basename, g)
        for g in data_globs
    )


def _collect_docstring_node_ids(tree: ast.AST) -> set[int]:
    """Return ids() of Constant nodes that ARE docstrings."""
    docstring_ids: set[int] = set()
    for node in ast.walk(tree):
        if isinstance(node, (ast.Module, ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef)):
            body = getattr(node, "body", [])
            if (
                body
                and isinstance(body[0], ast.Expr)
                and isinstance(body[0].value, ast.Constant)
                and isinstance(body[0].value.value, str)
            ):
                docstring_ids.add(id(body[0].value))
    return docstring_ids


def _check(ctx: RuleContext) -> Iterable[Finding]:
    """Yield findings for hardcoded model-name literals in non-data Python."""
    if not ctx.is_python or ctx.ast_tree is None:
        return
    if ctx.is_test_path:
        return
    settings = rule_settings(ctx.config, _RULE_ID)
    data_globs = settings.get("data_file_globs", []) or []
    if _is_data_file(ctx.file_path, data_globs):
        return
    custom_pattern = settings.get("model_name_pattern")
    pattern = re.compile(custom_pattern) if custom_pattern else _DEFAULT_PATTERN
    docstring_ids = _collect_docstring_node_ids(ctx.ast_tree)
    for node in ast.walk(ctx.ast_tree):
        if not isinstance(node, ast.Constant):
            continue
        if id(node) in docstring_ids:
            continue
        value = node.value
        if not isinstance(value, str):
            continue
        hit = _match_model_literal(value, pattern, bool(custom_pattern))
        if hit is None:
            continue
        line = node.lineno
        col = (node.col_offset or 0) + 1
        snippet = ctx.lines[line - 1] if 0 < line <= len(ctx.lines) else ""
        yield Finding(
            rule_id=_RULE_ID,
            severity="error",
            file=ctx.file_path,
            line=line,
            col=col,
            snippet=snippet,
            message=(
                f"hardcoded model-name literal {hit!r} in production code. "
                "Model names rot fast and silently break the catalog."
            ),
            fix_hint=(
                "Pass the model the operation or thread owns — the thread's "
                "model target, or an explicit model input supplied by the "
                "caller. Raise loudly if unset; never fall back to a string "
                "literal or a runtime default (issue #317)."
            ),
        )


def _match_model_literal(
    value: str, pattern: re.Pattern[str], has_custom_pattern: bool
) -> str | None:
    """Return the matched model id in ``value``, or None.

    Two match modes:
      1. Whole-string: the constant IS a model id (``model = 'anthropic/...'``).
      2. Embedded SQL DEFAULT: a quoted model id is the value of a SQL
         ``DEFAULT`` clause inside a larger string.
    """
    if pattern.match(value):
        return value
    if has_custom_pattern:
        return None
    embedded = _EMBEDDED_DDL_DEFAULT_PATTERN.search(value)
    if embedded:
        return embedded.group(1)
    return None


RULE = Rule(
    id=_RULE_ID,
    severity="error",
    description=(
        "Detects hardcoded model-name string literals (`anthropic/...`, "
        "`openai/...`, `hosted_vllm/...`, `lmstudio/...`, etc.) in "
        "production runtime code. Catalog/pricing data files, schema "
        "DDL, tests, and docstrings are excluded. Every turn carries its "
        "own model target and every operation takes its own model input; a "
        "runtime model default is itself a defect."
    ),
    false_positive_examples=[
        "Catalog data in `backend/llm/catalog.py` — defines what models exist.",
        "Pricing tables in `backend/llm/pricing.py` — pricing per model id.",
        "Capability tables in `backend/llm/capabilities.py`.",
        "Provider-prefix declarations in `backend/llm/local_backends.py`.",
        "Test fixtures pinning a specific model for reproducibility.",
        "Model names mentioned inside docstrings (allowed).",
    ],
    fix_hint=(
        "Take the model from the turn's target or the operation's own model "
        "input; raise loudly if unset. Never use a string-literal default or "
        "any runtime model fallback (issue #317)."
    ),
    check=_check,
    calibration_note=(
        "Sprint 3 closeout (2026-05-15). Discovered after the stale "
        "`LOCAL_MODEL = 'hosted_vllm/Qwen3.5-35B-A3B'` default in "
        "infra/config.py broke CI's e2e job with a 'model not found in "
        "catalog' error long after the actual model went away."
    ),
)
