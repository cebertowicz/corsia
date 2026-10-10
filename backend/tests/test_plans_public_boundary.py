"""Code outside ``backend/plans`` reaches plans only through ``plans.public`` (#2217).

Every other ``plans`` module is internal. A new outside caller that needs a
plans name adds it to ``plans/public.py`` rather than importing the module
that defines it, so the facade stays the whole of what plans promises.
"""

from __future__ import annotations

import ast
from collections.abc import Iterator
from pathlib import Path

BACKEND = Path(__file__).resolve().parents[1]
INTERNAL = ("plans", "tests")
FACADE = "plans.public"


def _outside_files() -> Iterator[Path]:
    for path in sorted(BACKEND.rglob("*.py")):
        parts = path.relative_to(BACKEND).parts
        if parts[0] in INTERNAL or any(part.startswith(".") for part in parts):
            continue
        yield path


def _plans_imports(tree: ast.AST) -> Iterator[tuple[int, str]]:
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom) and node.module:
            if node.module == "plans":
                for alias in node.names:
                    yield node.lineno, f"plans.{alias.name}"
            elif node.module.startswith("plans."):
                yield node.lineno, node.module
        elif isinstance(node, ast.Import):
            for alias in node.names:
                if alias.name == "plans" or alias.name.startswith("plans."):
                    yield node.lineno, alias.name


def test_outside_code_imports_only_the_plans_facade() -> None:
    leaks = [
        f"{path.relative_to(BACKEND)}:{lineno} imports {module}"
        for path in _outside_files()
        for lineno, module in _plans_imports(ast.parse(path.read_text()))
        if module != "plans.public"
    ]
    assert leaks == [], "import from plans.public instead:\n" + "\n".join(leaks)


def _module_name(path: Path) -> str:
    name = ".".join(path.relative_to(BACKEND).with_suffix("").parts)
    return name.removesuffix(".__init__")


def _import_time_edges(path: Path, known: set[str]) -> list[str]:
    """Modules ``path`` imports when it loads, skipping TYPE_CHECKING blocks."""
    edges = []
    for statement in ast.parse(path.read_text()).body:
        if isinstance(statement, ast.If) and "TYPE_CHECKING" in ast.dump(statement.test):
            continue
        nested = isinstance(statement, (ast.If, ast.Try))
        for node in ast.walk(statement) if nested else [statement]:
            if isinstance(node, ast.ImportFrom) and node.module and not node.level:
                edges.append(node.module)
                edges += [f"{node.module}.{alias.name}" for alias in node.names]
            elif isinstance(node, ast.Import):
                edges += [alias.name for alias in node.names]
    return [edge for edge in edges if edge in known]


def test_plans_never_loads_the_facade_while_it_loads() -> None:
    """An outside module that plans imports at load time cannot import the
    facade at load time: the facade would be half-built and the import fails.
    Such a module imports ``plans.public`` under TYPE_CHECKING or in a function.
    """
    paths = {
        _module_name(path): path for path in BACKEND.rglob("*.py")
        if path.relative_to(BACKEND).parts[0] != "tests"
        and not any(part.startswith(".") for part in path.relative_to(BACKEND).parts)
    }
    known = set(paths)
    edges = {name: _import_time_edges(path, known) for name, path in paths.items()}
    reached_by: dict[str, str] = {}
    pending = [name for name in edges if name.startswith("plans.") and name != FACADE]
    while pending:
        name = pending.pop()
        for target in edges[name]:
            if target != FACADE and target not in reached_by:
                reached_by[target] = name
                pending.append(target)
    cycles = sorted(
        f"{name} (loaded by {reached_by[name]}) imports {FACADE}"
        for name in reached_by
        if not name.startswith("plans") and FACADE in edges[name]
    )
    assert cycles == []


def test_the_scan_sees_outside_callers() -> None:
    callers = [
        path for path in _outside_files()
        if any(True for _ in _plans_imports(ast.parse(path.read_text())))
    ]
    assert len(callers) >= 30
