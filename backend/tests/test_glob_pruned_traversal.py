"""Glob skips excluded folders without reading them, and says when it skipped or stopped."""

from __future__ import annotations

import os
from pathlib import Path

import pytest

from tools import glob_walk
from tools.glob import GlobTool
from tools.glob_result import SKIPPED_NOTICE, folder_breakdown
from tools.search_scope import SearchScope, pattern_components

THREAD = "thread-current"
INCOMPLETE = "INCOMPLETE: stopped after examining {} entries; results are partial — narrow path or pattern"


class _CountingScandir:
    """Count every entry any ``os.scandir`` caller reads."""

    def __init__(self, real) -> None:
        self.real = real
        self.entries = 0

    def __call__(self, path):
        return _CountedIterator(self, self.real(path))


class _CountedIterator:
    def __init__(self, counter: _CountingScandir, iterator) -> None:
        self.counter = counter
        self.iterator = iterator

    def __enter__(self):
        return self

    def __exit__(self, *exc_info) -> None:
        self.iterator.close()

    def __iter__(self):
        return self

    def __next__(self):
        entry = next(self.iterator)
        self.counter.entries += 1
        return entry


class _CountingListdir:
    """Count every name the descriptor-bound walker lists."""

    def __init__(self, real) -> None:
        self.real = real
        self.entries = 0

    def __call__(self, path):
        names = self.real(path)
        self.entries += len(names)
        return names


def _write(root: Path, names: list[str]) -> None:
    for index, name in enumerate(names):
        path = root / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(name, encoding="utf-8")
        os.utime(path, (1_700_000_000 + index, 1_700_000_000 + index))


def _grow_skipped_subtrees(root: Path, count: int) -> None:
    _write(root, [f".worktrees/wt/app/copy{index}.py" for index in range(count)])
    _write(root, [f"node_modules/pkg/lib{index}.py" for index in range(count)])
    _write(root, [f"app/__pycache__/cached{index}.py" for index in range(count)])


def _glob_py(root: Path, reviewed: bool) -> str:
    if reviewed:
        return GlobTool._glob_reviewed("**/*.py", root, THREAD, False)
    return GlobTool(workspace=root)._glob_unreviewed("**/*.py", root, None, THREAD, False)


def _entries_read(monkeypatch, root: Path, reviewed: bool) -> tuple[int, str]:
    """Count entries read through either listing primitive during one glob."""
    listdir, scandir = _CountingListdir(os.listdir), _CountingScandir(os.scandir)
    with monkeypatch.context() as patch:
        patch.setattr(os, "listdir", listdir)
        patch.setattr(os, "scandir", scandir)
        output = _glob_py(root, reviewed)
    return listdir.entries + scandir.entries, output


@pytest.mark.parametrize("reviewed", [False, True])
def test_skipped_subtrees_are_never_read(monkeypatch, tmp_path, reviewed):
    root = tmp_path.resolve()
    _write(root, ["README.md", "app/main.py", "app/lib/util.py"])
    _grow_skipped_subtrees(root, 5)
    small_entries, small_output = _entries_read(monkeypatch, root, reviewed)
    _grow_skipped_subtrees(root, 200)
    large_entries, large_output = _entries_read(monkeypatch, root, reviewed)

    assert small_output == large_output == "app/lib/util.py\napp/main.py"
    assert large_entries == small_entries


@pytest.mark.parametrize("reviewed", [False, True])
def test_one_large_directory_is_read_only_up_to_the_budget(monkeypatch, tmp_path, reviewed):
    root = tmp_path.resolve()
    _write(root, [f"bulk/file{index}.py" for index in range(3_000)])
    monkeypatch.setattr(glob_walk, "ENTRY_BUDGET", 100)

    entries, output = _entries_read(monkeypatch, root, reviewed)

    assert output.startswith(INCOMPLETE.format(100))
    # One read past the budget is the entry that was refused.
    assert entries <= 101


def test_complete_result_is_only_the_listing_even_when_folders_were_skipped(tmp_path):
    root = tmp_path.resolve()
    _write(root, ["a.md", "docs/b.md", ".github/c.md", "node_modules/d.md"])
    tool = GlobTool(workspace=root)

    assert tool._glob_unreviewed("**/*.md", root, None, THREAD, False) == "docs/b.md\na.md"
    assert GlobTool._glob_reviewed("**/*.md", root, THREAD, False) == "docs/b.md\na.md"


def test_empty_result_names_what_was_not_searched(tmp_path):
    root = tmp_path.resolve()
    _write(root, [".github/workflows/ci.yml", "src/app.py"])
    tool = GlobTool(workspace=root)
    expected = f"No files matching '**/*.yml' in {root}\n{SKIPPED_NOTICE}"

    assert tool._glob_unreviewed("**/*.yml", root, None, THREAD, False) == expected
    assert GlobTool._glob_reviewed("**/*.yml", root, THREAD, False) == expected
    inside = root / ".github"
    assert tool._glob_unreviewed("**/*.yml", inside, None, THREAD, False) == "workflows/ci.yml"


def test_empty_result_without_skipped_folders_is_unchanged(tmp_path):
    root = tmp_path.resolve()
    _write(root, ["src/app.py"])
    tool = GlobTool(workspace=root)
    expected = f"No files matching '**/*.yml' in {root}"

    assert tool._glob_unreviewed("**/*.yml", root, None, THREAD, False) == expected
    assert GlobTool._glob_reviewed("**/*.yml", root, THREAD, False) == expected


def test_truncated_result_counts_matches_by_folder_and_names_skips(tmp_path):
    root = tmp_path.resolve()
    names = (
        [f"src/a/m{index}.py" for index in range(60)]
        + [f"src/b/m{index}.py" for index in range(40)]
        + [f"src/m{index}.py" for index in range(20)]
        + ["node_modules/pkg/skip.py"]
    )
    _write(root, names)
    tool = GlobTool(workspace=root)
    newest_first = list(reversed(names[:-1]))
    expected = (
        "\n".join(newest_first[:100])
        + "\n\n... and 20 more files\nMatches by folder:\n"
        + "- src/a/ (60)\n- src/b/ (40)\n- src/ (20 directly inside)\n"
        + SKIPPED_NOTICE
    )

    assert tool._glob_unreviewed("**/*.py", root, None, THREAD, False) == expected
    assert GlobTool._glob_reviewed("**/*.py", root, THREAD, False) == expected


def test_folder_breakdown_ranks_ten_folders_and_counts_the_rest():
    paths = [f"pkg{index}/mod{copy}.py" for index in range(12) for copy in range(index + 1)]
    paths += ["top.py"]

    assert folder_breakdown(paths).splitlines() == [
        "Matches by folder:",
        *[f"- pkg{index}/ ({index + 1})" for index in range(11, 1, -1)],
        "- (+3 more folders)",
    ]


@pytest.mark.parametrize("reviewed", [False, True])
def test_spent_budget_is_declared_incomplete(monkeypatch, tmp_path, reviewed):
    from tools.filesystem_effects import walk_bound_files

    root = tmp_path.resolve()
    _write(root, [f"pkg{index}/mod.py" for index in range(20)] + [".hid/x.py"])
    complete = _glob_py(root, reviewed)
    scope = SearchScope(THREAD, False, root_name=root.name, lists_env=True)
    traversal = glob_walk.Traversal(scope, components=pattern_components("**/*.py"))
    if reviewed:
        with walk_bound_files(
            root, skip_directory=traversal.prunes_directory, take_entry=traversal.take,
        ) as files:
            list(files)
    else:
        list(glob_walk.select_matches(root, "**/*.py", traversal))

    monkeypatch.setattr(glob_walk, "ENTRY_BUDGET", traversal.examined)
    assert _glob_py(root, reviewed) == complete

    monkeypatch.setattr(glob_walk, "ENTRY_BUDGET", 30)
    header, _, body = _glob_py(root, reviewed).partition("\n\n")
    assert header == INCOMPLETE.format(30)
    listed = [line for line in body.splitlines() if line.endswith(".py")]
    assert listed and set(listed) < set(complete.splitlines())
