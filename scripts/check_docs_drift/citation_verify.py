"""Existence verification for extracted citations against the current tree.

Route verification reads each ``backend/api/*.py`` file's own
``APIRouter(prefix=...)`` plus its own ``@router.<method>(...)`` decorators,
with one evidenced exception: a file with no module-level router that
defines a ``def register_x(router: APIRouter)``-shaped helper (for example
``model_inference_overrides.py``, called from ``models.py`` as
``register_inference_override_routes(router)``) borrows the prefix of
whichever file calls that helper. It does not walk ``include_router``
composition beyond that one pattern (for example the network-server
job/lifecycle sub-routers mounted from ``network_servers.py``). None of the
routes currently cited in the checked docs rely on that deeper composition,
but a future citation into a composed sub-router could false-positive as
missing.

Subcommand verification is flat across ``scripts/corsia``'s case-branch
nesting: it does not model bash case-block scoping, so a two-word citation
like ``corsia service status`` is verified as "both words are known labels
somewhere", not "status is nested specifically under service". A documented
soundness compromise, not a completeness one.
"""

from __future__ import annotations

import re
from pathlib import Path

_ROUTE_DECORATOR_RE = re.compile(r'@router\.[a-z]+\(\s*["\']([^"\']*)["\']')
_ROUTER_PREFIX_RE = re.compile(r'APIRouter\([^)]*prefix\s*=\s*["\']([^"\']*)["\']')
_HAS_OWN_ROUTER_RE = re.compile(r"^router\s*=\s*APIRouter\(", re.MULTILINE)
_REGISTER_FN_RE = re.compile(r"^def (\w+)\(\s*router\s*:\s*APIRouter", re.MULTILINE)
_PATH_PARAM_RE = re.compile(r"\{[^}]+\}")
# A bash `case` label, including pipe-alternated patterns like
# `install|uninstall|start)`: every alternative before the `)` is a real,
# independently dispatched label (evidenced in `scripts/corsia`'s nested
# `service` and `user|token` branches).
_CASE_LABEL_RE = re.compile(r"^\s*([a-z][a-z0-9_-]*(?:\|[a-z][a-z0-9_-]*)*)\)", re.MULTILINE)
_PYTHON_VERSION_RE = re.compile(r"python-version:\s*['\"]?([0-9.]+)")

# First tokens whose branch in `scripts/corsia` really does dispatch a
# second word through another bash `case` (evidenced: `service` nests
# install|uninstall|start|stop|restart|status; `logs` nests backend). Every
# other multi-word citation found in practice (`user ...`, `token ...`,
# `delegate ...`, `doc ...`, `context ...`) forwards its remaining words
# verbatim to a different program's own argument parser (`python -m cli`,
# `delegate_cli.py`, `doc_cli.py`, `context_cli.py`) -- the second word is
# not a `scripts/corsia` case label at all, and checking it there would be
# a false positive, not a real gap.
_NESTED_DISPATCH_COMMANDS = frozenset({"service", "logs"})

# Cited `/api/...` paths confirmed against the current tree to belong to a
# third party the docs also describe (the oMLX network-server's own health
# endpoint, probed by `backend/llm/network_discovery.py` and
# `backend/llm/network_endpoints.py`), not a route CORSIA itself serves
# under `backend/api/`. `/api/...` alone cannot distinguish "CORSIA's API"
# from "another service that happens to use the same REST convention"; this
# is the one case found in practice, not a general filter.
NON_CORSIA_ROUTES = frozenset({"/api/status"})


def missing_paths(candidates: set[str], repo_root: Path, fallback_dir: str | None = None) -> list[str]:
    """Cited repo-relative paths that do not exist, checking one fallback dir.

    Some docs (ARCHITECTURE.md) cite backend-internal paths without the
    `backend/` prefix, e.g. `infra/thread_inputs.py` for
    `backend/infra/thread_inputs.py` -- evidenced, not assumed: both files
    exist only under `backend/`, and the bare `infra/` top-level directory
    is unrelated (CI tooling only).
    """
    missing = []
    for candidate in sorted(candidates):
        if (repo_root / candidate).exists():
            continue
        if fallback_dir and (repo_root / fallback_dir / candidate).exists():
            continue
        missing.append(candidate)
    return missing


def _normalize_route(route: str) -> str:
    """Collapse every ``{name}`` or ``{name:converter}`` segment to ``{}``."""
    return _PATH_PARAM_RE.sub("{}", route)


def _own_prefixes(files: dict[str, str]) -> dict[str, str]:
    return {
        name: next(iter(_ROUTER_PREFIX_RE.findall(text)), "")
        for name, text in files.items()
        if _HAS_OWN_ROUTER_RE.search(text)
    }


def _inherited_prefix(name: str, text: str, files: dict[str, str], prefixes: dict[str, str]) -> str:
    """A file with no module-level router borrows its caller's prefix."""
    for fn_name in _REGISTER_FN_RE.findall(text):
        called_in = (
            other for other, other_text in files.items() if other != name and f"{fn_name}(" in other_text
        )
        for other in called_in:
            if other in prefixes:
                return prefixes[other]
    return ""


def registered_api_routes(api_dir: Path) -> set[str]:
    """Every ``/api/...`` route assembled from each file's prefix + decorators."""
    files = {p.name: p.read_text(encoding="utf-8") for p in sorted(api_dir.glob("*.py"))}
    prefixes = _own_prefixes(files)
    routes: set[str] = set()
    for name, text in files.items():
        prefix = prefixes.get(name)
        if prefix is None:
            prefix = _inherited_prefix(name, text, files, prefixes)
        for path in _ROUTE_DECORATOR_RE.findall(text):
            routes.add(_normalize_route(f"{prefix}{path}") or "/")
    return routes


def missing_routes(cited: set[str], api_dir: Path) -> list[str]:
    """Cited ``/api/...`` routes not found among the registered routes."""
    registered = registered_api_routes(api_dir)
    return sorted(
        r
        for r in cited
        if r not in NON_CORSIA_ROUTES and _normalize_route(r) not in registered
    )


def corsia_case_labels(corsia_script: Path) -> set[str]:
    """Every case-branch label anywhere in ``scripts/corsia``, alternatives split."""
    labels: set[str] = set()
    for group in _CASE_LABEL_RE.findall(corsia_script.read_text(encoding="utf-8")):
        labels.update(group.split("|"))
    return labels


def _unknown_tokens(tokens: tuple[str, ...], labels: set[str]) -> bool:
    """Whether a cited token tuple has an unverifiable word (see module docs)."""
    checked = tokens if tokens[0] in _NESTED_DISPATCH_COMMANDS else tokens[:1]
    return any(t not in labels for t in checked)


def missing_subcommands(
    cited: set[tuple[str, ...]], corsia_script: Path
) -> list[str]:
    """Cited ``corsia <word> [<word>]`` invocations with an unverifiable token."""
    labels = corsia_case_labels(corsia_script)
    missing = [
        " ".join(("corsia", *tokens)) for tokens in cited if _unknown_tokens(tokens, labels)
    ]
    return sorted(missing)


def python_version_mismatches(cited_version: str, workflows_dir: Path) -> list[str]:
    """Workflow files that pin a Python version other than the cited floor."""
    mismatches = []
    for wf in sorted(workflows_dir.glob("*.yml")):
        for pinned in _PYTHON_VERSION_RE.findall(wf.read_text(encoding="utf-8")):
            if pinned != cited_version:
                mismatches.append(f"{wf.name}: pins {pinned}, docs cite {cited_version}")
    return mismatches
