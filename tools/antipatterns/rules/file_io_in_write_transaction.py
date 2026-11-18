"""Flag direct file I/O in write holds; called functions are not followed.

Ambiguous open/rename/replace methods are checked only on plainly constructed
or annotated pathlib Paths within the same function; other receivers are missed.
"""

from __future__ import annotations

import ast
from typing import Iterable

from ..findings import Finding
from ._base import Rule, RuleContext
from .direct_commit_outside_write_transaction import _scope_name

_RULE_ID = "file-io-in-write-transaction"
_PATH_METHODS = {"read_text", "write_text", "read_bytes", "write_bytes", "unlink", "mkdir", "touch"}
_AMBIGUOUS = {"open", "rename", "replace"}
_OS_METHODS = {"replace", "rename", "remove", "unlink", "makedirs", "mkdir", "rmdir", "fsync"}
_SCOPES = (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef, ast.Lambda)
_FIX = "Move file I/O outside the write_transaction body. This rule checks direct calls only."
_EXCEPTIONS = {
    ("backend/connections/account_projection.py", "ensure_projection"),  # ordering fence with DB state; owner to move
    ("backend/connections/account_projection.py", "cleanup_stale"),  # ordering fence with DB state; owner to move
    ("backend/registry/durable_writes.py", "_register_applied"),  # ordering fence with DB state; owner to move
    ("backend/tools/conductor_instruction_create.py", "_install"),  # ordering fence with DB state; owner to move
}


def _imports(tree: ast.AST) -> tuple[set[str], dict[str, str]]:
    writers = {"write_transaction"}
    modules: dict[str, str] = {}
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                if alias.name in {"os", "shutil", "json", "pickle", "pathlib"}:
                    modules[alias.asname or alias.name] = alias.name
        elif isinstance(node, ast.ImportFrom):
            for alias in node.names:
                if alias.name == "write_transaction" and (node.module or "").endswith("database"):
                    writers.add(alias.asname or alias.name)
                elif node.module in {"os", "shutil", "json", "pickle", "pathlib"}:
                    modules[alias.asname or alias.name] = f"{node.module}.{alias.name}"
    return writers, modules


def _inside_writer(node: ast.AST, parents: dict[ast.AST, ast.AST], writers: set[str]) -> bool:
    while node in parents:
        parent = parents[node]
        if isinstance(parent, _SCOPES):
            return False
        if isinstance(parent, (ast.With, ast.AsyncWith)) and node in parent.body:
            for item in parent.items:
                expr = item.context_expr
                if isinstance(expr, ast.Call) and (
                    isinstance(expr.func, ast.Name) and expr.func.id in writers
                    or isinstance(expr.func, ast.Attribute) and expr.func.attr == "write_transaction"
                ):
                    return True
        node = parent
    return False


def _path_type(node: ast.AST, modules: dict[str, str]) -> bool:
    if isinstance(node, ast.Name):
        return modules.get(node.id) == "pathlib.Path"
    return (isinstance(node, ast.Attribute) and node.attr == "Path"
            and isinstance(node.value, ast.Name) and modules.get(node.value.id) == "pathlib")


def _path_expr(node: ast.AST, names: set[str], modules: dict[str, str]) -> bool:
    if isinstance(node, ast.Name):
        return node.id in names
    if isinstance(node, ast.Call):
        return _path_type(node.func, modules)
    if isinstance(node, ast.BinOp) and isinstance(node.op, ast.Div):
        return _path_expr(node.left, names, modules)
    return False


def _path_names(node: ast.AST, parents: dict[ast.AST, ast.AST], modules: dict[str, str]) -> set[str]:
    scope = node
    while scope in parents and not isinstance(scope, (ast.FunctionDef, ast.AsyncFunctionDef)):
        scope = parents[scope]
    if not isinstance(scope, (ast.FunctionDef, ast.AsyncFunctionDef)):
        return set()
    # Only consider nodes owned by this function, not nested definitions.
    def owned(item: ast.AST) -> bool:
        current = item
        while current in parents and parents[current] is not scope:
            current = parents[current]
            if isinstance(current, _SCOPES):
                return False
        return current in parents

    nodes = [item for item in ast.walk(scope) if owned(item)]
    names = {arg.arg for arg in nodes if isinstance(arg, ast.arg) and arg.annotation and _path_type(arg.annotation, modules)}
    changed = True
    while changed:
        before = len(names)
        for item in nodes:
            if isinstance(item, ast.AnnAssign) and isinstance(item.target, ast.Name):
                if _path_type(item.annotation, modules) or (item.value and _path_expr(item.value, names, modules)):
                    names.add(item.target.id)
            elif isinstance(item, ast.Assign) and _path_expr(item.value, names, modules):
                names.update(target.id for target in item.targets if isinstance(target, ast.Name))
        changed = len(names) != before
    return names


def _file_call(node: ast.Call, modules: dict[str, str], names: set[str]) -> bool:
    func = node.func
    if isinstance(func, ast.Name):
        name = modules.get(func.id, func.id)
        return (name == "open" or name.startswith("shutil.")
                or name in {f"os.{n}" for n in _OS_METHODS}
                or (name in {"json.dump", "pickle.dump"} and _has_file_arg(node)))
    if not isinstance(func, ast.Attribute):
        return False
    if isinstance(func.value, ast.Name):
        module = modules.get(func.value.id)
        if module == "shutil":
            return True
        if module == "os":
            return func.attr in _OS_METHODS
        if module in {"json", "pickle"}:
            return func.attr == "dump" and _has_file_arg(node)
    return func.attr in _PATH_METHODS or (func.attr in _AMBIGUOUS and _path_expr(func.value, names, modules))


def _has_file_arg(node: ast.Call) -> bool:
    return len(node.args) >= 2 or any(k.arg in {"fp", "file"} for k in node.keywords)


def _check(ctx: RuleContext) -> Iterable[Finding]:
    if not ctx.is_python or not ctx.file_path.startswith("backend/") or ctx.ast_tree is None:
        return
    writers, modules = _imports(ctx.ast_tree)
    parents = {child: parent for parent in ast.walk(ctx.ast_tree) for child in ast.iter_child_nodes(parent)}
    for node in ast.walk(ctx.ast_tree):
        if not isinstance(node, ast.Call) or not _inside_writer(node, parents, writers):
            continue
        if (ctx.file_path, _scope_name(node, parents)) in _EXCEPTIONS:
            continue
        if _file_call(node, modules, _path_names(node, parents, modules)):
            yield Finding(rule_id=_RULE_ID, severity="error", file=ctx.file_path,
                          line=node.lineno, col=node.col_offset + 1,
                          snippet=ctx.lines[node.lineno - 1] if ctx.lines else "",
                          message="Direct file I/O inside a write_transaction body; called functions are not checked.",
                          fix_hint=_FIX)


RULE = Rule(id=_RULE_ID, severity="error", check=_check, fix_hint=_FIX,
            description="Reject direct file I/O while a write transaction is held.",
            false_positive_examples=["Methods with file-like names on non-filesystem objects."],
            calibration_note="Lexical direct calls only; ambiguous methods require a plainly identified Path.")
