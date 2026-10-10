from __future__ import annotations

import inspect
import os
import sys
from pathlib import Path

import pytest
import yaml

sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))

from eval.scorers.resolve import SCORER_REGISTRY



_REPO_ROOT = Path(__file__).resolve().parents[2]
_CANONICAL_DIR = _REPO_ROOT / "evals" / "scenarios" / "canonical"


def _canonical_files() -> list[Path]:
    if not _CANONICAL_DIR.exists():
        return []
    return sorted(_CANONICAL_DIR.glob("*.yaml"))


pytestmark = pytest.mark.skipif(
    not _canonical_files(),
    reason=f"No canonical scenarios at {_CANONICAL_DIR}",
)



def _parse_scorer_decl(raw: object) -> tuple[str | None, dict]:
    if isinstance(raw, str):
        return raw, {}
    if isinstance(raw, dict):
        name = raw.get("name")
        if not isinstance(name, str) or not name:
            return None, {}
        args = raw.get("args") or {}
        if not isinstance(args, dict):
            return None, {}
        return name, dict(args)
    return None, {}


def _scenario_scorer_decls(
    yaml_path: Path,
) -> list[tuple[int, str, dict]]:
    data = yaml.safe_load(yaml_path.read_text()) or {}
    decls = data.get("scorers") or []
    out: list[tuple[int, str, dict]] = []
    for i, raw in enumerate(decls):
        name, args = _parse_scorer_decl(raw)
        if name is None:
            continue
        out.append((i, name, args))
    return out


def _scenario_malformed_decls(yaml_path: Path) -> list[tuple[int, object]]:
    data = yaml.safe_load(yaml_path.read_text()) or {}
    decls = data.get("scorers") or []
    out: list[tuple[int, object]] = []
    for i, raw in enumerate(decls):
        name, _args = _parse_scorer_decl(raw)
        if name is None:
            out.append((i, raw))
    return out


def _scenario_pass_if_refs(yaml_path: Path) -> list[str]:
    data = yaml.safe_load(yaml_path.read_text()) or {}
    pass_if = data.get("pass_if") or {}
    if not isinstance(pass_if, dict):
        return []
    refs: list[str] = []
    seen: set[str] = set()
    for key in ("all_of", "any_of"):
        for n in pass_if.get(key) or []:
            if isinstance(n, str) and n not in seen:
                refs.append(n)
                seen.add(n)
    return refs



def _all_scorer_refs() -> list[tuple[Path, int, str, dict]]:
    refs: list[tuple[Path, int, str, dict]] = []
    for f in _canonical_files():
        for idx, name, args in _scenario_scorer_decls(f):
            refs.append((f, idx, name, args))
    return refs


def _all_malformed_decls() -> list[tuple[Path, int, object]]:
    refs: list[tuple[Path, int, object]] = []
    for f in _canonical_files():
        for idx, raw in _scenario_malformed_decls(f):
            refs.append((f, idx, raw))
    return refs


def _all_pass_if_refs() -> list[tuple[Path, str]]:
    refs: list[tuple[Path, str]] = []
    for f in _canonical_files():
        for n in _scenario_pass_if_refs(f):
            refs.append((f, n))
    return refs


_SCORER_REFS = _all_scorer_refs()
_SCORER_REF_IDS = [
    f"{f.name}::scorers[{idx}]::{name}" for f, idx, name, _ in _SCORER_REFS
]

_MALFORMED_DECLS = _all_malformed_decls()
_MALFORMED_DECL_IDS = [
    f"{f.name}::scorers[{idx}]" for f, idx, _ in _MALFORMED_DECLS
]

_PASS_IF_REFS = _all_pass_if_refs()
_PASS_IF_REF_IDS = [f"{f.name}::pass_if::{name}" for f, name in _PASS_IF_REFS]



@pytest.mark.parametrize(
    "yaml_path,idx,name,args",
    _SCORER_REFS,
    ids=_SCORER_REF_IDS,
)
def test_scorer_name_registered(
    yaml_path: Path, idx: int, name: str, args: dict,
) -> None:
    assert name in SCORER_REGISTRY, (
        f"\nScorer name {name!r} (in {yaml_path.name} scorers[{idx}]) "
        f"is not in SCORER_REGISTRY.\n\n"
        f"Fix one of:\n"
        f"  • Register the scorer in backend/eval/scorers/resolve.py "
        f"(callable if implemented, ``None`` if pending).\n"
        f"  • Correct the name in {yaml_path.name} if it's a typo.\n"
        f"  • Remove the scorer reference if it's stale.\n"
    )


@pytest.mark.parametrize(
    "yaml_path,idx,name,args",
    _SCORER_REFS,
    ids=_SCORER_REF_IDS,
)
def test_scorer_kwargs_bind(
    yaml_path: Path, idx: int, name: str, args: dict,
) -> None:
    factory = SCORER_REGISTRY.get(name)
    if factory is None:
        pytest.skip(
            f"{name} is a registered stub (None); no factory to bind."
        )

    bind_errors: list[str] = []

    if args:
        try:
            factory(**args)
        except TypeError as e:
            bind_errors.append(f"factory(**args) raised TypeError: {e}")
        else:
            return  # success
    try:
        factory()
    except TypeError as e:
        bind_errors.append(f"factory() raised TypeError: {e}")
    else:
        return  # success

    sig = inspect.signature(factory)
    raise AssertionError(
        f"\nScorer {name!r} (in {yaml_path.name} scorers[{idx}]) "
        f"cannot be instantiated with the declared args.\n\n"
        f"Declared args: {args!r}\n"
        f"Factory signature: {sig}\n\n"
        f"Attempts:\n"
        + "".join(f"  • {e}\n" for e in bind_errors)
        + f"\nFix one of:\n"
        f"  • Update {yaml_path.name} args to match the factory signature.\n"
        f"  • Update the factory signature in "
        f"backend/eval/scorers/registry/ to match the YAML.\n"
    )


@pytest.mark.parametrize(
    "yaml_path,idx,raw",
    _MALFORMED_DECLS,
    ids=_MALFORMED_DECL_IDS,
)
def test_scorer_declaration_well_formed(
    yaml_path: Path, idx: int, raw: object,
) -> None:
    raise AssertionError(
        f"\nMalformed scorer declaration in {yaml_path.name} scorers[{idx}]:\n\n"
        f"  raw: {raw!r}\n\n"
        f"Expected one of:\n"
        f"  • a plain string (the scorer name)\n"
        f"  • a dict with non-empty 'name' key, "
        f"optionally with 'args' as a dict\n"
    )


@pytest.mark.parametrize(
    "yaml_path,name",
    _PASS_IF_REFS,
    ids=_PASS_IF_REF_IDS,
)
def test_pass_if_references_listed_scorer(
    yaml_path: Path, name: str,
) -> None:
    declared_names = {
        decl_name for _, decl_name, _ in _scenario_scorer_decls(yaml_path)
    }
    assert name in declared_names, (
        f"\npass_if in {yaml_path.name} references scorer {name!r}, "
        f"but it's not in the scenario's scorers: list.\n\n"
        f"Declared scorers: {sorted(declared_names)}\n\n"
        f"Fix one of:\n"
        f"  • Add {name!r} to the scorers: list in {yaml_path.name}.\n"
        f"  • Remove {name!r} from pass_if (if it was renamed or stale).\n"
    )
