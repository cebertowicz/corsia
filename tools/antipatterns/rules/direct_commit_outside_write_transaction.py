"""Guard the completed writer census without changing runtime transaction owners."""

from __future__ import annotations

import ast
import re
from pathlib import PurePosixPath
from typing import Iterable

from ..findings import Finding
from ._base import Rule, RuleContext

_RULE_ID = "direct-commit-outside-write-transaction"
_EXCLUDED = {
    "backend/infra/database.py",  # The canonical transaction implementation.
    "backend/api/project_repos.py",  # #1077: separate repository redesign.
    # Its own file (otel_logs.db) with one flock-guarded writer thread; never
    # corsia.db, whose lock-holder census write_transaction reports.
    "backend/infra/otel_logs/store.py",
    "backend/eval/fixtures/blank_session.py",  # Fixture setup/teardown only.
    "backend/e2e_mock.py",
    "backend/e2e_app.py",
    "backend/e2e_pressure_fixture.py",
}
_READ_SNAPSHOTS = {
    # Owned mode=ro connection; closes before immutable Git reads, never a writer.
    ("backend/core/lesson_report_db.py", "report_snapshot"),
    ("backend/api/thread_cache_warm.py", "_warm_replay_candidates"),
    ("backend/api/thread_paging.py", "read_authorized_page"),
    ("backend/infra/runtime_truth.py", "provider_boundary_snapshot"),
    ("backend/infra/runtime_truth.py", "_has_registered_provider"),
    ("backend/infra/send_replay_reads.py", "read_candidates"),
    ("backend/api/checkpoints.py", "_manual_context"),
    ("backend/core/checkpoint_agent_admission.py", "read_checkpoint_preparation"),
    ("backend/core/turn_iteration_context.py", "_read_native_checkpoint_context"),
    ("backend/thread/model_checkpoint.py", "read_switch_checkpoint_context"),
    ("backend/thread/idle_checkpoint.py", "read_idle_context"),
    ("backend/subagents/harness_checkpoint_context.py", "read_external_checkpoint_context"),
    ("backend/thread/model_source.py", "_read_model_switch_start"),
    ("backend/thread/model_source.py", "_read_model_switch_candidates"),
    # Read-only deferred snapshot that rolls back; the write follows in write_transaction.
    ("backend/plans/status_drain.py", "_read_owed"),
}
_SCOPES = (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef, ast.Lambda)
_TRANSACTION_SQL = re.compile(r"^\s*(BEGIN|COMMIT)\b", re.IGNORECASE)
_FIX_HINT = (
    "Use the existing infra.database.write_transaction owner at the complete "
    "SQL boundary; keep filesystem, provider and async work outside it."
)


def _inside_writer(node: ast.AST, parents: dict[ast.AST, ast.AST]) -> bool:
    """Only the with body owns calls; nested definitions start a new scope."""
    while node in parents:
        parent = parents[node]
        if isinstance(parent, _SCOPES):
            return False
        if isinstance(parent, ast.With) and node in parent.body:
            for item in parent.items:
                expression = item.context_expr
                if isinstance(expression, ast.Call):
                    function = expression.func
                    if (isinstance(function, ast.Name) and function.id == "write_transaction"
                            or isinstance(function, ast.Attribute) and function.attr == "write_transaction"):
                        return True
        node = parent
    return False


def _scope_name(node: ast.AST, parents: dict[ast.AST, ast.AST]) -> str:
    """Qualify scopes so an approved top-level reader cannot exempt a namesake."""
    names = []
    while node in parents:
        node = parents[node]
        if isinstance(node, _SCOPES):
            names.append(getattr(node, "name", "<lambda>"))
    return ".".join(reversed(names))


def _boundary(node: ast.Call) -> str | None:
    if not isinstance(node.func, ast.Attribute):
        return None
    if node.func.attr == "commit":
        return "commit()"
    if node.func.attr not in {"execute", "executescript"} or not node.args:
        return None
    argument = node.args[0]
    if isinstance(argument, ast.Constant) and isinstance(argument.value, str):
        if _TRANSACTION_SQL.match(argument.value):
            return " ".join(argument.value.upper().split()).rstrip(";").strip()
    return None


def _finding(ctx: RuleContext, line: int, col: int, message: str) -> Finding:
    return Finding(
        rule_id=_RULE_ID, severity="error", file=ctx.file_path,
        line=line, col=col, snippet=ctx.lines[line - 1] if ctx.lines else "",
        message=message, fix_hint=_FIX_HINT,
    )


def _check(ctx: RuleContext) -> Iterable[Finding]:
    path = PurePosixPath(ctx.file_path)
    if (not ctx.is_python or not ctx.file_path.startswith("backend/")
            or {"tests", "__pycache__"}.intersection(path.parts)
            or ctx.file_path in _EXCLUDED):
        return
    if ctx.ast_tree is None:
        yield _finding(ctx, 1, 1, "Cannot parse Python to verify its transaction boundaries.")
        return
    parents = {child: parent for parent in ast.walk(ctx.ast_tree)
               for child in ast.iter_child_nodes(parent)}
    for node in ast.walk(ctx.ast_tree):
        if not isinstance(node, ast.Call):
            continue
        boundary = _boundary(node)
        if boundary is None or _inside_writer(node, parents):
            continue
        if (boundary == "BEGIN" and isinstance(node.func, ast.Attribute)
                and node.func.attr == "execute"
                and (ctx.file_path, _scope_name(node, parents)) in _READ_SNAPSHOTS):
            continue
        yield _finding(
            ctx, node.lineno, node.col_offset + 1,
            f"Private transaction boundary {boundary!r} outside a with write_transaction(...) body.",
        )


RULE = Rule(
    id=_RULE_ID, severity="error", check=_check, fix_hint=_FIX_HINT,
    description="Reject private commits and literal BEGIN/COMMIT outside the canonical writer body.",
    false_positive_examples=[
        "The canonical transaction implementation and established non-production fixtures.",
        "Named plain-BEGIN read snapshots; their commits remain checked.",
        "Repository-route transaction redesign remains an explicit #1077 dependency.",
    ],
    calibration_note=(
        "G-final: enabled after all 117 in-scope sites were converted. "
        "Lexical calls/literal SQL only, not dynamic SQL or connection-identity analysis."
    ),
)
