"""Measure tracked source files and functions for the code-health ratchet.

Canonical home of the code-health standards and of the path classification
that decides what the ratchet governs. Other collectors import from here
rather than restating the numbers, so there is one executable definition of
"too big" in the repository.

File sizes are measured for every governed kind (.py, .ts, .svelte). Function
spans are measured for Python only, from the AST. TypeScript and Svelte
function limits are deliberately absent: obtaining spans without a real
parser means hand-rolled lexing of comments, strings, nested template
interpolation and regex literals, and a lexer that is wrong in either
direction is worse than no check -- it either invents functions that cannot
be split or fails closed on valid source and blocks unrelated work. Measured
against this repository, that machinery would govern 4 entries against 259
Python ones. Issue #1260 tracks doing it properly through the frontend's own
toolchain (TypeScript compiler API or an ESLint rule), reporting into this
same baseline. File sizes for .ts and .svelte are unaffected and still
enforced here.
"""

from __future__ import annotations

import ast
import pathlib
import subprocess
from collections import Counter

# The code-health standards these entries are being worked down toward.
STANDARDS = {".py": 400, ".ts": 300, ".svelte": 400}
FUNCTION_STANDARD = 80

_SKIP_PARTS = frozenset(
    {
        ".git",
        ".svelte-kit",
        ".venv",
        ".worktrees",
        "__pycache__",
        "build",
        "coverage",
        "dist",
        "generated",
        "node_modules",
        "vendor",
        "venv",
    }
)


def standard_for(name: str) -> int | None:
    """Return the line standard for this file kind, or None when unscanned."""
    return STANDARDS.get(pathlib.PurePosixPath(name).suffix)


def is_test_path(relative: str) -> bool:
    """Return whether a repo-relative path is test-only source.

    This is the union of the two rules that previously disagreed -- the
    ratchet's and the health harness's -- so neither can drift from the other.
    Any basename containing "conftest" counts, not just conftest.py itself:
    narrowing that would silently promote fixture modules to production source
    for a now-blocking gate.

    ``relative`` must already be relative to the repo root. An ancestor
    outside the repo happening to be named ``tests`` (a common checkout- or
    CI-workspace directory name) must never affect this classification.
    """
    path = pathlib.PurePosixPath(relative)
    name = path.name
    return (
        bool({"tests", "__tests__", "e2e"}.intersection(path.parts))
        or "conftest" in name
        or name.startswith("test_")
        or name.endswith(
            ("_test.py", ".test.ts", ".test.svelte", ".spec.ts", ".spec.svelte")
        )
    )


def count_lines(raw: bytes) -> int:
    """Count physical lines without requiring the source to decode."""
    return len(raw.splitlines())


def is_source(relative: str) -> bool:
    """Return whether a tracked path is governed production source."""
    path = pathlib.PurePosixPath(relative)
    return (
        path.suffix in STANDARDS
        and not _SKIP_PARTS.intersection(path.parts)
        and not is_test_path(relative)
    )


def owning_file(name: str) -> str:
    """Return the file an entry belongs to; a function key is 'path::name'."""
    return name.split("::", 1)[0]


def tracked_paths(root: pathlib.Path) -> list[str]:
    """Return every Git-tracked path, in Git's deterministic order.

    A failure here is never a silent empty scan: an empty file list would make
    every limit look deleted and every check pass.
    """
    result = subprocess.run(
        ["git", "-C", str(root), "ls-files", "-z"],
        check=False,
        capture_output=True,
    )
    if result.returncode:
        detail = result.stderr.decode("utf-8", "replace").strip()
        raise RuntimeError(
            f"cannot list tracked source files: {detail or 'git ls-files failed'}"
        )
    return [
        item.decode("utf-8", "surrogateescape")
        for item in result.stdout.split(b"\0")
        if item
    ]


def python_functions(relative: str, text: str) -> tuple[dict[str, int], str | None]:
    """Parse Python strictly; malformed source is a gate failure, not a skip."""
    try:
        tree = ast.parse(text, filename=relative)
    except SyntaxError as exc:
        return {}, f"{relative}: Python syntax error: {exc.msg}"

    values: dict[str, int] = {}
    seen: Counter[str] = Counter()

    def walk(node: ast.AST, scope: list[str]) -> None:
        """Record spans under stable lexical scope, not source position."""
        for child in ast.iter_child_nodes(node):
            if isinstance(child, ast.ClassDef):
                walk(child, [*scope, child.name])
            elif isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef)):
                base = ".".join([*scope, child.name])
                seen[base] += 1
                key = base if seen[base] == 1 else f"{base}#{seen[base]}"
                values[f"{relative}::{key}"] = child.end_lineno - child.lineno + 1
                walk(child, [*scope, child.name])
            else:
                walk(child, scope)

    walk(tree, [])
    return values, None


def functions_in(relative: str, text: str) -> tuple[dict[str, int], str | None]:
    """Return function spans for one source file, or an explicit parse failure.

    Only Python is parsed. A non-Python file contributes no function entries
    and no error: an absent measurement is honest, where a guessed one is not.
    """
    if relative.endswith(".py"):
        return python_functions(relative, text)
    return {}, None


def measure(
    root: pathlib.Path,
) -> tuple[dict[str, int], dict[str, int], list[str], list[tuple[str, str]]]:
    """Return (file sizes, function sizes, unreadable paths, parse failures).

    A file that cannot be read or decoded is never silently dropped: it is
    reported separately so the caller can fail loudly instead of letting an
    unmeasurable file evade the ratchet, or a tracked-but-unreadable file get
    misreported as deleted. A file whose functions cannot be parsed is
    reported the same way, for the same reason, as an explicit (path, detail)
    pair so no caller has to recover the path by re-parsing the message.
    """
    sizes: dict[str, int] = {}
    functions: dict[str, int] = {}
    unreadable: list[str] = []
    parse_errors: list[tuple[str, str]] = []
    for relative in tracked_paths(root):
        if not is_source(relative):
            continue
        try:
            raw = (root / relative).read_bytes()
        except OSError:
            unreadable.append(relative)
            continue
        sizes[relative] = count_lines(raw)
        try:
            text = raw.decode("utf-8")
        except UnicodeDecodeError:
            unreadable.append(relative)
            continue
        found, error = functions_in(relative, text)
        functions.update(found)
        if error:
            parse_errors.append((relative, error))
    return sizes, functions, sorted(unreadable), sorted(parse_errors)


def over_standard(sizes: dict[str, int], standard) -> dict[str, int]:
    """Keep only entries that exceed the standard for their kind."""
    return {
        name: lines for name, lines in sizes.items() if lines > (standard(name) or 0)
    }
