"""Glob and grep search the current thread's folder and folders a search names.

The workspace is a git repository whose ``.gitignore`` ignores ``.threads/``, as
the production workspace's does, so grep's ripgrep path must reach the thread's
own folder through its scope globs, not through ignore rules.
"""

from __future__ import annotations

import asyncio
import subprocess
import sys
from types import SimpleNamespace

import pytest

from tools.glob import GlobTool
from tools.glob_result import SKIPPED_NOTICE
from tools.grep import GrepTool
from tools.search_scope import THREADS_NOTICE

CURRENT = "thread-current"
OTHER = "thread-other"

FILES = {
    "src/app.py": "NEEDLE app",
    f".threads/{CURRENT}/working/mine.md": "NEEDLE mine",
    f".threads/{OTHER}/working/theirs.md": "NEEDLE theirs",
    f".threads/{OTHER}/uploads/upload.md": "NEEDLE upload",
    ".worktrees/feature/src/branch.py": "NEEDLE branch",
    ".github/workflows/ci.yml": "NEEDLE ci",
    "node_modules/pkg/index.js": "NEEDLE package",
    ".env": "NEEDLE secret",
    ".gitlab-ci.yml": "NEEDLE pipeline",
    # A thread folder whose only file is hidden: found only when a filter names it.
    f"projects/p/.threads/{CURRENT}/.gitlab-ci.yml": "NEEDLE thread pipeline",
}


@pytest.fixture()
def workspace(tmp_path, monkeypatch):
    from tools import sandbox_config

    root = (tmp_path / "ws").resolve()
    for name, text in FILES.items():
        path = root / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text + "\n", encoding="utf-8")
    (root / ".gitignore").write_text(".threads/\n.worktrees/\n", encoding="utf-8")
    subprocess.run(["git", "init", "-q", str(root)], check=True)
    monkeypatch.setattr(sandbox_config, "WORKSPACE", root)
    monkeypatch.setattr(sandbox_config, "_STATIC_ALLOWED_RW", [root])
    monkeypatch.setattr(sandbox_config, "_STATIC_ALLOWED_RO", [])
    monkeypatch.setattr(sandbox_config, "_list_authoritative_repo_policies", lambda: [])
    return root


def _lines(output: str) -> set[str]:
    return {line for line in output.splitlines() if line and not line.startswith("Not searched")}


async def _glob(root, pattern, **options):
    return await GlobTool(workspace=root).execute(pattern=pattern, _thread_id=CURRENT, **options)


async def _grep(root, monkeypatch, ripgrep, **options):
    import tools.grep as grep_module

    if ripgrep and not grep_module._RG_AVAILABLE:
        pytest.skip("ripgrep not available")
    monkeypatch.setattr(grep_module, "_RG_AVAILABLE", ripgrep)
    return await GrepTool(workspace=root).execute(
        pattern="NEEDLE", output_mode="files", _thread_id=CURRENT, **options,
    )


async def test_glob_finds_own_thread_files_and_only_named_skipped_folders(workspace):
    everything = _lines(await _glob(workspace, "**/*"))
    assert f".threads/{CURRENT}/working/mine.md" in everything
    assert "src/app.py" in everything and ".env" in everything
    assert ".gitlab-ci.yml" not in everything
    assert not {name for name in everything if name.startswith((".worktrees", ".github", "node_modules"))}
    assert not {name for name in everything if OTHER in name}

    assert _lines(await _glob(workspace, ".worktrees/*/src/*.py")) == {".worktrees/feature/src/branch.py"}
    assert _lines(await _glob(workspace, "**/.github/**/*.yml")) == {".github/workflows/ci.yml"}
    assert _lines(await _glob(workspace, "node_modules/*/*.js")) == {"node_modules/pkg/index.js"}
    assert _lines(await _glob(workspace, ".gitlab-ci.yml")) == {".gitlab-ci.yml"}


async def test_glob_reaches_other_threads_only_with_all_threads(workspace):
    named = await _glob(workspace, ".threads/*/working/*.md")
    assert _lines(named) == {f".threads/{CURRENT}/working/mine.md"}
    everything = _lines(await _glob(workspace, ".threads/**/*.md", all_threads=True))
    assert everything == {
        f".threads/{CURRENT}/working/mine.md",
        f".threads/{OTHER}/working/theirs.md",
        f".threads/{OTHER}/uploads/upload.md",
    }


async def test_searches_started_inside_thread_folders_keep_isolation(workspace, monkeypatch):
    other = workspace / ".threads" / OTHER
    assert await _glob(workspace, "**/*.md", path=str(other)) == (
        f"No files matching '**/*.md' in {other}\n{THREADS_NOTICE}"
    )
    assert _lines(await _glob(workspace, "**/*.md", path=str(other), all_threads=True)) == {
        "working/theirs.md", "uploads/upload.md",
    }
    container = workspace / ".threads"
    assert _lines(await _glob(workspace, "**/*.md", path=str(container))) == {f"{CURRENT}/working/mine.md"}
    for ripgrep in (True, False):
        assert await _grep(workspace, monkeypatch, ripgrep, path=str(other)) == (
            f"No matches for pattern 'NEEDLE'\n{THREADS_NOTICE}"
        )
        assert _lines(await _grep(workspace, monkeypatch, ripgrep, path=str(container))) == {
            f"{CURRENT}/working/mine.md",
        }


@pytest.mark.parametrize("ripgrep", [True, False])
async def test_grep_reads_own_thread_folder_even_when_gitignored(workspace, monkeypatch, ripgrep):
    hits = _lines(await _grep(workspace, monkeypatch, ripgrep))
    assert hits == {"src/app.py", f".threads/{CURRENT}/working/mine.md"}

    everything = _lines(await _grep(workspace, monkeypatch, ripgrep, all_threads=True))
    assert everything == hits | {
        f".threads/{OTHER}/working/theirs.md", f".threads/{OTHER}/uploads/upload.md",
    }


@pytest.mark.parametrize("ripgrep", [True, False])
async def test_grep_glob_filter_that_names_a_skipped_folder_searches_it(workspace, monkeypatch, ripgrep):
    assert _lines(await _grep(workspace, monkeypatch, ripgrep, glob=".github/**/*.yml")) == {
        ".github/workflows/ci.yml",
    }
    assert _lines(await _grep(workspace, monkeypatch, ripgrep, glob="node_modules/**/*.js")) == {
        "node_modules/pkg/index.js",
    }
    assert _lines(await _grep(workspace, monkeypatch, ripgrep, glob="src/*.py")) == {"src/app.py"}
    assert _lines(await _grep(workspace, monkeypatch, ripgrep, glob="*.{py,yml}")) == {"src/app.py"}
    assert _lines(await _grep(workspace, monkeypatch, ripgrep, glob=".gitlab-ci.yml")) == {
        ".gitlab-ci.yml", f"projects/p/.threads/{CURRENT}/.gitlab-ci.yml",
    }
    assert _lines(await _grep(workspace, monkeypatch, ripgrep, glob="!*.py")) == {
        f".threads/{CURRENT}/working/mine.md",
    }


async def test_reviewed_grep_searches_what_its_filter_names(workspace, monkeypatch):
    from tools import host_access_gate

    (workspace / ".worktrees/feature/node_modules/dep").mkdir(parents=True)
    (workspace / ".worktrees/feature/node_modules/dep/dep.js").write_text("NEEDLE\n", encoding="utf-8")

    async def reviewed_permit(**_kwargs):
        return SimpleNamespace(resolved_path=workspace)

    monkeypatch.setattr(host_access_gate, "consume_current_action_permit", reviewed_permit)

    async def reviewed(**options):
        return _lines(await GrepTool(workspace=workspace).execute(
            pattern="NEEDLE", path=str(workspace), output_mode="files", _thread_id=CURRENT, **options,
        ))

    # The reviewed walk enters hidden folders, as it always has, but not other
    # threads' folders or unnamed caches and worktrees.
    assert await reviewed() == {
        "src/app.py", f".threads/{CURRENT}/working/mine.md", ".github/workflows/ci.yml",
    }
    assert await reviewed(glob="node_modules/**/*.js") == {"node_modules/pkg/index.js"}
    assert await reviewed(glob=".worktrees/*/src/*.py") == {".worktrees/feature/src/branch.py"}
    assert await reviewed(glob=".gitlab-ci.yml") == {".gitlab-ci.yml", f"projects/p/.threads/{CURRENT}/.gitlab-ci.yml"}
    assert await reviewed(glob="!*.yml") == {"src/app.py", f".threads/{CURRENT}/working/mine.md"}


async def test_ripgrep_honors_gitignore_outside_thread_folders(workspace, monkeypatch):
    for name in ("build/out.py", f"projects/p/.threads/{CURRENT}/PLAN.md", f"projects/p/.threads/{OTHER}/PLAN.md"):
        path = workspace / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("NEEDLE\n", encoding="utf-8")
    (workspace / ".gitignore").write_text(".threads/\n.worktrees/\nbuild/\nprojects/*/.threads/\n", encoding="utf-8")

    assert _lines(await _grep(workspace, monkeypatch, True)) == {
        "src/app.py", f".threads/{CURRENT}/working/mine.md", f"projects/p/.threads/{CURRENT}/PLAN.md",
    }
    assert _lines(await _grep(workspace, monkeypatch, True, glob="*.md")) == {
        f".threads/{CURRENT}/working/mine.md", f"projects/p/.threads/{CURRENT}/PLAN.md",
    }


async def test_ripgrep_failures_are_reported_never_empty(workspace, monkeypatch):
    import os

    if os.geteuid() == 0:
        pytest.skip("permission failures need a non-root user")
    complete = await _grep(workspace, monkeypatch, True)  # skips without ripgrep
    assert _lines(complete) == {"src/app.py", f".threads/{CURRENT}/working/mine.md"}

    bad_regex = await GrepTool(workspace=workspace).execute(pattern="(unclosed", _thread_id=CURRENT)
    assert bad_regex.startswith("Error: ripgrep failed:\n") and "regex parse error" in bad_regex

    # The thread-folder listing fails, so the current thread's files are missing:
    # the search says so instead of passing off the rest as complete.
    threads = workspace / ".threads"
    threads.chmod(0)
    try:
        partial = await _grep(workspace, monkeypatch, True)
    finally:
        threads.chmod(0o755)
    first_line, blank, *hits = partial.splitlines()
    assert first_line.startswith("INCOMPLETE: ripgrep could not search every path (")
    assert first_line.endswith("Permission denied (os error 13)); results may be partial")
    assert (blank, hits) == ("", ["src/app.py"])


async def test_ripgrep_process_ends_with_its_search(monkeypatch):
    from tools import grep_ripgrep

    started = []
    spawn_process = asyncio.create_subprocess_exec

    async def spawn(*command, **options):
        started.append(await spawn_process(*command, **options))
        return started[-1]

    monkeypatch.setattr(grep_ripgrep.asyncio, "create_subprocess_exec", spawn)
    with pytest.raises(TimeoutError):
        async with asyncio.timeout(0.2):
            await grep_ripgrep._run([sys.executable, "-c", "import time; time.sleep(30)"])

    assert [process.returncode is not None for process in started] == [True]


def test_walk_never_lists_other_threads_folders(tmp_path, monkeypatch):
    from tools import glob_walk
    from tools.search_scope import SearchScope

    root = tmp_path.resolve()
    for index in range(200):
        path = root / ".threads" / OTHER / "working" / f"note{index}.md"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("x", encoding="utf-8")
    (root / ".threads" / CURRENT).mkdir()
    (root / ".threads" / CURRENT / "mine.md").write_text("x", encoding="utf-8")
    listed: list[str] = []
    real_scandir = glob_walk.os.scandir

    def scandir(directory):
        listed.append(str(directory))
        return real_scandir(directory)

    monkeypatch.setattr(glob_walk.os, "scandir", scandir)
    traversal = glob_walk.Traversal(SearchScope(CURRENT, False, root_name=root.name))
    selected = list(glob_walk.select_matches(root, "**/*.md", traversal))

    assert selected == [(".threads", CURRENT, "mine.md")]
    assert not [directory for directory in listed if OTHER in directory]
    assert traversal.skipped_threads


async def test_empty_glob_names_what_it_did_not_search(workspace):
    output = await _glob(workspace, "**/*.yml")
    assert output == f"No files matching '**/*.yml' in {workspace}\n{SKIPPED_NOTICE}\n{THREADS_NOTICE}"
