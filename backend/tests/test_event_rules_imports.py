"""B4: the event-rule modules import one another in one direction, with no deferred import
standing in for a cycle (``rules`` <-> ``rule_runs`` <-> ``rule_authoring`` before 2.B-2)."""

from __future__ import annotations

import ast
from pathlib import Path

_EVENTS = Path(__file__).resolve().parents[1] / "events"
_MODULES = sorted(p.stem for p in _EVENTS.glob("*.py") if p.stem.startswith("rule") or p.stem == "worker_starts")


def _imports(name: str) -> dict[str, bool]:
    """Each rule module this one imports, and whether that import sits inside a function."""
    tree = ast.parse((_EVENTS / f"{name}.py").read_text())
    found: dict[str, bool] = {}

    def visit(node: ast.AST, deferred: bool) -> None:
        if isinstance(node, ast.If) and "TYPE_CHECKING" in ast.unparse(node.test):
            return  # typing only: never runs
        if isinstance(node, ast.ImportFrom) and node.module and node.module.startswith("events"):
            targets = ([node.module.split(".")[1]] if node.module != "events"
                       else [alias.name for alias in node.names])
            for target in targets:
                if target in _MODULES and target != name:
                    found[target] = found.get(target, False) or deferred
        inner = deferred or isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
        for child in ast.iter_child_nodes(node):
            visit(child, inner)

    visit(tree, False)
    return found


def test_the_rule_modules_import_each_other_at_top_level_and_without_a_cycle():
    graph = {name: _imports(name) for name in _MODULES}
    assert {"rules", "rule_runs", "rule_authoring", "worker_starts"} <= set(graph)
    deferred = [(name, target) for name, targets in graph.items() for target, late in targets.items() if late]
    assert deferred == []

    done: set[str] = set()

    def walk(name: str, path: tuple[str, ...]) -> None:
        assert name not in path, f"import cycle: {' -> '.join((*path, name))}"
        if name in done:
            return
        for target in graph[name]:
            walk(target, (*path, name))
        done.add(name)

    for name in graph:
        walk(name, ())
