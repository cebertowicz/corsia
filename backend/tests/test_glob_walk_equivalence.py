"""Pruned glob traversal returns exactly what an unpruned glob filtered by scope returns.

The references are the glob tool's two traversal methods before pruning, with
their old skip filters replaced by the search scope's rule
(``tools.search_scope.scoped_match``): pathlib's own ``glob`` for ordinary paths
and a full bound walk for reviewed ones. The pruned walkers must select exactly
what those references keep. Every file gets a distinct modification time, so
output order is fully decided by the tool's own sort and the comparison is
byte-for-byte once the notices (folder counts and not-searched lines, which only
empty or truncated results carry) are removed; their text is pinned in
``test_glob_pruned_traversal.py``.
"""

from __future__ import annotations

import os
from pathlib import Path

import pytest

from tools import glob as glob_module
from tools.filesystem_effects import match_relative_glob, walk_bound_files
from tools.glob import GlobTool
from tools.glob_result import SKIPPED_NOTICE
from tools.search_scope import THREADS_NOTICE, SearchScope, keep_path, scoped_match

CURRENT = "thread-current"
OTHER = "thread-other"

FILES = (
    "top.py", "README.md", "notes.MD", "Makefile", ".env", ".gitignore",
    "linktarget.txt", "ab.py", "bb.py", "cb.py", "q1.py", "stop.py",
    "a/two.py", "a/b/one.py", "a/a/b.py", "a/c/deep.txt",
    "linked/l.py", "src/index.ts", "src/x/c.ts", "src/x/y/d.tsx", "Docs/Guide.md",
    ".hid/y/h.py", ".github/workflows/ci.yml", "config/.env/inner.py", "sub/.env",
    "node_modules/pkg/index.js", "frontend/node_modules/lib/a.ts", "frontend/app.ts",
    "__pycache__/m.pyc", "backend/__pycache__/x.pyc", "backend/api.py",
    "venv/lib/site.py", ".venv/lib/site.py",
    f".threads/{CURRENT}/working/mine.md", f".threads/{OTHER}/working/theirs.md",
    f".threads/{OTHER}/context/ctx.md", ".threads/.DS_Store", "threads/plain/notes.md",
    ".worktrees/wt/src/w.py", ".worktrees/wt/node_modules/n.js",
)
LINKS = {
    "filelink.py": "linktarget.txt",
    "a/dirlink": "../linked",
    "broken.py": "missing.py",
}

UNREVIEWED_PATTERNS = (
    "*", "*.md", "*.MD", "**/*.py", "src/**/*.ts", "SRC/**/*.ts", "**/*.{ts,tsx}",
    "**/", "**", "a/**", "a/*", "?1.py", "[ab]*", "[!a]*.py", "**/.github/**/*.yml",
    ".github/workflows/*.yml", ".env", "**/.env", "config/.env/*.py",
    "**/node_modules/**/*.js", "node_modules/*/*.js", "*/dirlink/*.py",
    "**/dirlink/*.py", "escape_dir/*.py", "**/escape_dir/*", "escape_file.md",
    "broken.py", "**/*", "**/**/*.py", "**/a/**/*.py", "a/../top.py", "./top.py",
    "top.py/", "TOP.PY", "**.py", "*/**.py", "", ".", "./", "/abs",
    "threads/**/*.md", "**/working/*.md", "a//b/*.py", "a/./b/*.py", "docs/*",
    ".threads/**/*.md", ".threads/*/working/*.md", f".threads/{OTHER}/*/*.md", ".*/**/*.py",
    ".worktrees/*/src/*.py", ".worktrees/**/*.js", "**/.threads/*", ".hid/*/*.py",
)
REVIEWED_PATTERNS = (
    "*", "*.md", "**/*.py", "src/**/*.ts", "**/*.{ts,tsx}", "**", "a/*", "[ab]*",
    ".env", "**/.env", "**/*.yml", "**/*.js", "**/*", "threads/**/*.md", "**/*.pyc",
    ".threads/**/*.md", "**/working/*.md", ".github/workflows/*.yml", ".worktrees/*/src/*.py",
    "node_modules/*/*.js", ".{github,hid}/**/*",
)


def _scope(search_dir, current_thread_id, all_threads):
    return SearchScope(current_thread_id, all_threads, root_name=search_dir.name, lists_env=True)


def _reference_unreviewed(tool, pattern, search_dir, requested_path, current_thread_id, all_threads):
    path = requested_path
    try:
        if not search_dir.exists():
            return f"Error: Directory not found: {path or str(tool._workspace)}"
        if not search_dir.is_dir():
            return f"Error: Not a directory: {path or str(tool._workspace)}"

        matches: list[tuple[Path, Path]] = []
        for match in search_dir.glob(pattern):
            resolved_match = match.resolve(strict=False)
            if not resolved_match.is_relative_to(search_dir):
                try:
                    resolved_match = glob_module._resolve_path(
                        str(match), tool._workspace, tool._allowed_dir
                    )
                except PermissionError:
                    continue
            if resolved_match.is_file():
                parts = match.relative_to(search_dir).parts
                if not scoped_match(parts, pattern, _scope(search_dir, current_thread_id, all_threads)):
                    continue
                if not keep_path(resolved_match, current_thread_id, all_threads):
                    continue
                matches.append((match, resolved_match))

        if not matches:
            return f"No files matching '{pattern}' in {search_dir}"

        matches.sort(key=lambda item: item[1].stat().st_mtime, reverse=True)

        lines = []
        for match, _resolved in matches[:100]:
            try:
                rel = match.relative_to(search_dir)
                lines.append(str(rel))
            except ValueError:
                lines.append(str(match))

        result = "\n".join(lines)
        if len(matches) > 100:
            result += f"\n\n... and {len(matches) - 100} more files"
        return result
    except PermissionError as e:
        return f"Error: {e}"


def _reference_reviewed(pattern, search_dir, current_thread_id, all_threads):
    scope = _scope(search_dir, current_thread_id, all_threads)
    matches: list[tuple[Path, float]] = []
    with walk_bound_files(search_dir) as files:
        for entry in files:
            relative = entry.relative
            if not match_relative_glob(relative, pattern):
                continue
            if not scoped_match(relative.parts, pattern, scope):
                continue
            matches.append((relative, entry.file_stat.st_mtime))

    if not matches:
        return f"No files matching '{pattern}' in {search_dir}"

    matches.sort(key=lambda item: item[1], reverse=True)
    lines = [str(relative) for relative, _mtime in matches[:100]]
    result = "\n".join(lines)
    if len(matches) > 100:
        result += f"\n\n... and {len(matches) - 100} more files"
    return result


def _as_tool_output(run, *args) -> str:
    """Format exceptions the way ``GlobTool.execute`` does."""
    try:
        return run(*args)
    except Exception as exc:
        return f"Error: {exc}"


def _listing(output: str) -> str:
    """Drop the notices so the plain listing remains."""
    for notice in (THREADS_NOTICE, SKIPPED_NOTICE):
        if output.endswith("\n" + notice):
            output = output[: -len(notice) - 1]
    head, marker, _counts = output.partition("\nMatches by folder:\n")
    return head if marker else output


def build_tree(root: Path, outside: Path) -> None:
    """Write the equivalence tree with one distinct modification time per file."""
    written = [*FILES, "outside:secret.md", "outside:tree/t.py"]
    for index, name in enumerate(written):
        path = outside / name.split(":", 1)[1] if name.startswith("outside:") else root / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(name, encoding="utf-8")
        os.utime(path, (1_700_000_000 + index, 1_700_000_000 + index))
    for link, target in LINKS.items():
        (root / link).symlink_to(target)
    (root / "escape_file.md").symlink_to(outside / "secret.md")
    (root / "escape_dir").symlink_to(outside / "tree")


class _Sandbox:
    """Stand-in path boundary: only the outside ``tree`` folder is allowed."""

    def __init__(self, allowed: Path) -> None:
        self.allowed = allowed

    def __call__(self, path, _workspace=None, _allowed_dir=None, **_kwargs) -> Path:
        resolved = Path(path).resolve(strict=False)
        if not resolved.is_relative_to(self.allowed):
            raise PermissionError(f"Path {path} is outside the sandbox.")
        return resolved


@pytest.fixture()
def tree(tmp_path, monkeypatch):
    root = tmp_path / "root"
    outside = tmp_path / "outside"
    build_tree(root, outside)
    monkeypatch.setattr(glob_module, "_resolve_path", _Sandbox((outside / "tree").resolve()))
    return root.resolve()


def _without_repeats(output: str) -> str:
    seen: set[str] = set()
    kept = []
    for line in output.splitlines():
        if line and line in seen:
            continue
        seen.add(line)
        kept.append(line)
    return "\n".join(kept)


@pytest.mark.parametrize("scope", [(CURRENT, False), (None, False), (CURRENT, True)])
@pytest.mark.parametrize("pattern", UNREVIEWED_PATTERNS)
def test_unreviewed_glob_selects_what_scoped_pathlib_selects(tree, pattern, scope):
    tool = GlobTool(workspace=tree)
    args = (pattern, tree, None, *scope)
    expected = _as_tool_output(_reference_unreviewed, tool, *args)
    if pattern.split("/").count("**") > 1:
        # Python 3.13's Path.glob can repeat a path reached through two '**'
        # segments; the pruned walk selects each path once, as 3.11 does.
        expected = _without_repeats(expected)
    assert _listing(_as_tool_output(tool._glob_unreviewed, *args)) == expected


@pytest.mark.parametrize("scope", [(CURRENT, False), (None, False), (CURRENT, True)])
@pytest.mark.parametrize("pattern", REVIEWED_PATTERNS)
def test_reviewed_glob_selects_what_a_scoped_full_walk_selects(tree, pattern, scope):
    expected = _as_tool_output(_reference_reviewed, pattern, tree, *scope)
    assert _listing(_as_tool_output(GlobTool._glob_reviewed, pattern, tree, *scope)) == expected


@pytest.mark.parametrize("pattern", ["*.md", "**/*.py", "workflows/*.yml", "**/*"])
def test_searching_inside_a_skipped_folder_selects_the_same(tree, pattern):
    tool = GlobTool(workspace=tree)
    for search_dir in (
        tree / ".github", tree / "node_modules", tree / ".hid", tree / ".threads",
        tree / ".threads" / CURRENT,
    ):
        args = (pattern, search_dir, None, CURRENT, False)
        expected = _as_tool_output(_reference_unreviewed, tool, *args)
        assert _listing(_as_tool_output(tool._glob_unreviewed, *args)) == expected
        reviewed = (pattern, search_dir, CURRENT, False)
        assert _listing(_as_tool_output(GlobTool._glob_reviewed, *reviewed)) == _as_tool_output(
            _reference_reviewed, *reviewed
        )


def test_many_matches_keep_the_same_listing_and_remainder_count(tmp_path):
    root = tmp_path / "wide"
    for index in range(140):
        path = root / f"pkg{index % 7}" / f"mod{index}.py"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("x", encoding="utf-8")
        os.utime(path, (1_700_000_000 + index, 1_700_000_000 + index))
    root = root.resolve()
    tool = GlobTool(workspace=root)
    new = tool._glob_unreviewed("**/*.py", root, None, CURRENT, False)
    old = _reference_unreviewed(tool, "**/*.py", root, None, CURRENT, False)
    assert _listing(new) == old and new != old
    reviewed = GlobTool._glob_reviewed("**/*.py", root, CURRENT, False)
    assert _listing(reviewed) == _reference_reviewed("**/*.py", root, CURRENT, False)


def test_equal_modification_times_select_the_same_files(tmp_path):
    root = tmp_path / "ties"
    for name in ("b/z.py", "a/y.py", "x.py", "a/c/w.py"):
        path = root / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("x", encoding="utf-8")
        os.utime(path, (1_700_000_000, 1_700_000_000))
    root = root.resolve()
    tool = GlobTool(workspace=root)
    new = tool._glob_unreviewed("**/*.py", root, None, CURRENT, False)
    old = _reference_unreviewed(tool, "**/*.py", root, None, CURRENT, False)
    assert sorted(new.splitlines()) == sorted(old.splitlines())
