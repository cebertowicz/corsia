"""Regex extraction of citable tokens from doc prose.

Deliberately conservative: a backtick span is treated as a path only when
its first path segment (or the whole span, for a bare root file) matches a
real top-level entry of the repo; as a route only when it starts with
``/api/`` immediately after an optional HTTP method; and as a ``corsia``
subcommand only in the single-span form `` `corsia <word> [<word>]` ``.
Slash/pipe-alternated citations split across multiple backtick spans (for
example `` `corsia start` / `stop` / `restart` ``, or
`` `corsia delegate|doc|context` ``) are not expanded -- a known,
documented gap. Missing a citation is silent; misjudging a recognized one is
not, so recognition stays narrow on purpose.
"""

from __future__ import annotations

import re

_BACKTICK_RE = re.compile(r"`([^`\n]+)`")
_CORSIA_CMD_RE = re.compile(r"^corsia\s+([a-z][\w-]*)(?:\s+([a-z][\w-]*))?$")
_ROUTE_RE = re.compile(r"^(?:[A-Z]+\s+)?(/api/[\w{}/.:-]*)")
# The exact-version Python citation lives in a `| Need | Notes |` requirements
# table row, e.g. `| Python 3.11 | CI and the test suite use exactly 3.11. |`.
_PYTHON_CITATION_RE = re.compile(r"^\|\s*Python\s+([0-9]+\.[0-9]+)\s*\|", re.MULTILINE)

# `namespace/check-name`-shaped GitHub commit-status context names: they
# share a real top-level directory's name syntactically (`qa/`) but are not
# filesystem paths. Evidenced in this repo's own docs (`qa/llm-reviewer` is
# the merge-gate status context, described there as a "status"/"gate", never
# opened as a file); not a general rule, just the one shape found in practice.
_NON_PATH_TOKENS = frozenset({"qa/llm-reviewer", "qa/override-reviewer"})


def backtick_spans(text: str) -> list[str]:
    """Every span of text enclosed in single backticks."""
    return _BACKTICK_RE.findall(text)


def extract_path_candidates(text: str, top_level: set[str]) -> set[str]:
    """Backtick tokens that look like repo-relative paths, by top-level name."""
    candidates = set()
    for span in backtick_spans(text):
        if span in _NON_PATH_TOKENS:
            continue
        if any(ch.isspace() for ch in span) or span.startswith(("~", "/", "http")):
            continue
        first_segment = span.split("/", 1)[0]
        if first_segment in top_level or span in top_level:
            candidates.add(span)
    return candidates


def extract_routes(text: str) -> set[str]:
    """Backtick-cited ``/api/...`` routes, with any HTTP method prefix dropped."""
    routes = set()
    for span in backtick_spans(text):
        match = _ROUTE_RE.match(span)
        if match:
            routes.add(match.group(1))
    return routes


def extract_corsia_subcommands(text: str) -> set[tuple[str, ...]]:
    """Single-span `` `corsia <word> [<word>]` `` citations, as token tuples."""
    tokens = set()
    for span in backtick_spans(text):
        match = _CORSIA_CMD_RE.match(span)
        if match:
            tokens.add(tuple(g for g in match.groups() if g))
    return tokens


def extract_cited_python_version(text: str) -> str | None:
    """The exact Python version cited in a `| Python X.Y | ... |` requirements row."""
    match = _PYTHON_CITATION_RE.search(text)
    return match.group(1) if match else None
