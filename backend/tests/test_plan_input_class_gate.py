"""The coverage gate over the plan input-class taxonomy.

`test_plan_input_class_sites.py` drives one case per raise site. This file reads
the tree instead: it resolves each raise's class at import time rather than
matching a name, so a *subclass* raised at a new site is found too, and then
asserts the set of sites and the set of cases are the same set.

That is the part the previous regression net was missing. Its rows were written
per operation, so 17 of 19 converted sites had no case at all and could be
reverted silently. A gate that only samples cannot tell you that.
"""

from __future__ import annotations

import ast
import builtins
import importlib
import pathlib

from plans import service
from tests.plan_input_class_cases import _CASES

PLANS = pathlib.Path(__file__).resolve().parents[1] / "plans"


def _modules():
    """Import every module in the plans package alongside its source."""
    for path in sorted(PLANS.glob("*.py")):
        name = "plans" if path.stem == "__init__" else f"plans.{path.stem}"
        yield path, importlib.import_module(name), path.read_text(encoding="utf-8")


def _raised_class(module, func: ast.expr):
    """Resolve a raise's callee against its module, then the builtins it sees."""
    if isinstance(func, ast.Name):
        found = getattr(module, func.id, None)
        return found if found is not None else getattr(builtins, func.id, None)
    if isinstance(func, ast.Attribute) and isinstance(func.value, ast.Name):
        return getattr(getattr(module, func.value.id, None), func.attr, None)
    return None


def _message(source: str, call: ast.Call) -> str:
    """Return the raise's own argument source, whitespace-normalised."""
    if not call.args:
        return ""
    return " ".join(ast.get_source_segment(source, call.args[0]).split())


def _input_class_names() -> set[str]:
    """Every name the input class is reachable by anywhere in the package."""
    names = {service.PlanInputError.__name__}
    for _path, module, _source in _modules():
        names.update(
            attribute
            for attribute, value in vars(module).items()
            if isinstance(value, type) and issubclass(value, service.PlanInputError)
        )
    return names


def _scan() -> tuple[list[tuple[str, str]], list[str]]:
    """Return every input-class raise site, and every one the gate cannot see.

    An unresolved callee only matters when it *names* an input class: several
    modules import the conflict class inside a function to break a cycle, and
    missing those costs nothing because they are not what this gate protects.
    """
    input_names = _input_class_names()
    sites: list[tuple[str, str]] = []
    invisible: list[str] = []
    for path, module, source in _modules():
        for node in ast.walk(ast.parse(source)):
            if not isinstance(node, ast.Raise) or not isinstance(node.exc, ast.Call):
                continue
            raised = _raised_class(module, node.exc.func)
            if isinstance(raised, type):
                if issubclass(raised, service.PlanInputError):
                    sites.append((path.name, _message(source, node.exc)))
                continue
            written = ast.get_source_segment(source, node.exc.func)
            if written.rsplit(".", maxsplit=1)[-1] in input_names:
                invisible.append(f"{path.name}: {written}")
    return sites, invisible


def test_no_input_class_raise_is_invisible_to_the_gate():
    """An input class imported inside a function body cannot be resolved from
    the module, so its site would drop out of the coverage check silently.
    Import the input class at module scope so the gate can see it."""
    _, invisible = _scan()

    assert invisible == []


def test_no_two_sites_in_one_module_share_a_message():
    """Cases are keyed by message, so two identical messages in one module would
    let one case stand in for both. Say them differently."""
    sites, _ = _scan()

    assert sorted(sites) == sorted(set(sites))


def test_every_input_class_site_has_a_case_and_every_case_has_a_site():
    """A sample is not a net. A refusal written without a case is a refusal that
    can be silently reverted, which is how 17 of 19 slipped."""
    sites, _ = _scan()

    assert set(sites) == set(_CASES)


BACKEND = PLANS.parent


def _handler_types(path: pathlib.Path, qualified: str) -> list[str]:
    """Return the exception types one `Class.method` or function catches."""
    tree = ast.parse(path.read_text(encoding="utf-8"))
    owner, _, function = qualified.rpartition(".")
    scopes = [tree] if not owner else [
        node for node in ast.walk(tree)
        if isinstance(node, ast.ClassDef) and node.name == owner
    ]
    found = [
        node for scope in scopes for node in ast.walk(scope)
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
        and node.name == function
    ]
    assert len(found) == 1, f"{path.name}: expected one {qualified}"
    return [
        ast.unparse(handler.type)
        for node in ast.walk(found[0])
        if isinstance(node, ast.Try)
        for handler in node.handlers
        if handler.type is not None
    ]


def test_both_mutation_surfaces_classify_by_the_same_single_class():
    """The 409/422 split has to be one class, not a tuple each surface keeps
    its own copy of. `LaneOwnedNodeError` was a sibling listed only on the HTTP
    handler, so the tool fell through to the conflict branch and told an agent
    to re-read and retry a refusal re-reading cannot fix. A tuple here is that
    bug waiting to happen again; ordering pins input-before-conflict."""
    expected = ["PermissionError", "public.PlanInputError", "public.PlanError"]

    assert _handler_types(
        BACKEND / "api" / "plans.py", "_mutation_operation",
    ) == expected
    assert _handler_types(
        BACKEND / "tools" / "plans.py", "PlanUpdateTool.execute",
    ) == expected
