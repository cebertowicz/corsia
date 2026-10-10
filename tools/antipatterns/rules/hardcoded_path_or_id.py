"""Rule: hardcoded-path-or-id.

Detects path-shaped or UUID-shaped string literals in code where they
shouldn't appear. Path examples: `/Users/...`, `/home/...`, `~/.corsia`.
UUID example: `9f5e2c1b-...`.

Allowed locations (skipped):
    - Files matching `config_path_globs` (config.py, settings.py, fixtures).
    - Files matching this rule's own `allow_files` setting: an explicit,
      reviewed per-file exemption for code that legitimately owns a
      canonical copy of a path-shaped pattern (e.g. a detector that must
      hold the same `/Users/` pattern this rule itself checks for).
    - String literals inside docstrings — the AST already keeps these
      separate from regular Constants when the string is the first stmt
      of a module/class/function; we treat them as docstrings.
    - String literals that are exclusively used as module/class docstrings
      or as `__doc__`-style overrides.

Severity: error per spec; calibration may downgrade to warning.
"""

from __future__ import annotations

import ast
import fnmatch
import re
from typing import Iterable

from ..config import rule_settings
from ..findings import Finding
from ._base import Rule, RuleContext


_RULE_ID = "hardcoded-path-or-id"


def _is_config_path(rel_path: str, config_globs: list[str]) -> bool:
    """True if this file lives at one of the allowed config locations."""
    basename = rel_path.rsplit("/", 1)[-1]
    return any(
        fnmatch.fnmatch(rel_path, g) or fnmatch.fnmatch(basename, g)
        for g in config_globs
    )


def _is_allowed_file(rel_path: str, allow_globs: list[str]) -> bool:
    """True if this file matches the rule's own explicit `allow_files` exemption."""
    return any(fnmatch.fnmatch(rel_path, g) for g in allow_globs)


def _collect_docstring_node_ids(tree: ast.AST) -> set[int]:
    """Return ids() of Constant nodes that ARE docstrings.

    A docstring is the first statement of a module / class / function /
    async function whose value is a string Constant. We exclude these
    Constants from the path-literal scan so authors can quote `/Users/...`
    in a docstring example without tripping the rule.
    """
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


def _collect_uuid_namespace_node_ids(tree: ast.AST) -> set[int]:
    """Return UUID literals that define explicitly named namespaces.

    UUIDv5 namespaces are stable code-level identity inputs, not references to
    persisted data rows. Keep the exception deliberately structural and narrow:
    only ``*_NAMESPACE = uuid.UUID(<literal>)`` is exempt.
    """
    namespace_ids: set[int] = set()
    for node in ast.walk(tree):
        if not isinstance(node, ast.Assign) or len(node.targets) != 1:
            continue
        target = node.targets[0]
        call = node.value
        if (
            not isinstance(target, ast.Name)
            or not target.id.endswith("_NAMESPACE")
            or not isinstance(call, ast.Call)
            or not isinstance(call.func, ast.Attribute)
            or not isinstance(call.func.value, ast.Name)
            or call.func.value.id != "uuid"
            or call.func.attr != "UUID"
            or len(call.args) != 1
            or call.keywords
            or not isinstance(call.args[0], ast.Constant)
            or not isinstance(call.args[0].value, str)
        ):
            continue
        namespace_ids.add(id(call.args[0]))
    return namespace_ids


def _path_match(value: str, patterns: list[re.Pattern]) -> bool:
    """True if `value` matches any of the compiled path patterns."""
    return any(p.search(value) for p in patterns)


def _check(ctx: RuleContext) -> Iterable[Finding]:
    """Yield findings for hardcoded paths or UUIDs in non-config Python."""
    if not ctx.is_python or ctx.ast_tree is None:
        return
    # Tests legitimately use synthetic paths like `/Users/test/...` as fixture
    # data; calibration showed test files are the dominant FP source.
    if ctx.is_test_path:
        return
    settings = rule_settings(ctx.config, _RULE_ID)
    config_globs = ctx.config.get("global", {}).get("config_path_globs", [])
    if _is_config_path(ctx.file_path, config_globs):
        return
    allow_globs = settings.get("allow_files", []) or []
    if _is_allowed_file(ctx.file_path, allow_globs):
        return
    raw_path_patterns = settings.get("path_patterns", []) or []
    raw_uuid_pattern = settings.get("uuid_pattern", "") or ""
    path_patterns = [re.compile(p) for p in raw_path_patterns]
    uuid_re = re.compile(raw_uuid_pattern) if raw_uuid_pattern else None
    docstring_ids = _collect_docstring_node_ids(ctx.ast_tree)
    uuid_namespace_ids = _collect_uuid_namespace_node_ids(ctx.ast_tree)
    for node in ast.walk(ctx.ast_tree):
        if not isinstance(node, ast.Constant):
            continue
        if id(node) in docstring_ids:
            continue
        if id(node) in uuid_namespace_ids:
            continue
        value = node.value
        if not isinstance(value, str):
            continue
        # Skip f-string component constants that contain only formatting.
        if not value.strip():
            continue
        line = node.lineno
        col = (node.col_offset or 0) + 1
        snippet = ctx.lines[line - 1] if 0 < line <= len(ctx.lines) else ""
        if path_patterns and _path_match(value, path_patterns):
            yield Finding(
                rule_id=_RULE_ID,
                severity="warning",
                file=ctx.file_path,
                line=line,
                col=col,
                snippet=snippet,
                message=(
                    f"hardcoded path literal {value!r} outside a config "
                    "file. Move to config or pass via injection."
                ),
                fix_hint=(
                    "Read the path from config (`config.local.json`, "
                    "`backend/infra/config.py`) or pass it as a function "
                    "argument; do not embed user-home paths in code."
                ),
            )
        if uuid_re is not None and uuid_re.search(value):
            yield Finding(
                rule_id=_RULE_ID,
                severity="warning",
                file=ctx.file_path,
                line=line,
                col=col,
                snippet=snippet,
                message=(
                    f"hardcoded UUID literal {value!r} outside config / "
                    "fixtures. UUIDs in production code couple to data."
                ),
                fix_hint=(
                    "Move UUIDs into a fixture, config, or test setup; "
                    "production code should generate UUIDs at runtime."
                ),
            )


RULE = Rule(
    id=_RULE_ID,
    severity="warning",
    description=(
        "Detects path-shaped string literals (`/Users/`, `/home/`, "
        "`~/.corsia`) and UUID-shaped literals in non-config Python "
        "files. Config files, fixtures, and docstrings are excluded."
    ),
    false_positive_examples=[
        "Paths inside docstrings (allowed).",
        "Paths inside test fixture YAML / JSON files (skipped via config_path_globs).",
        "`~/.corsia/` references in `AGENTS.md`-class docs (excluded).",
    ],
    fix_hint=(
        "Move the literal to config or pass it via injection; production "
        "code should not name user-home paths or specific UUIDs."
    ),
    check=_check,
)
