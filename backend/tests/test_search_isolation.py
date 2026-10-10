"""Search isolation for grep and glob in the real ``.threads/<id>`` layout.

Default file-search scope is shared files plus the current thread's own
``.threads/<id>`` folder, hidden and gitignored as it is. Another thread's whole
folder is excluded unless ``all_threads=True``, including when a search starts
inside it.
"""

from __future__ import annotations

import asyncio

import pytest

from tools.grep import GrepTool
from tools.glob import GlobTool
from tools.search_scope import THREADS_NOTICE

_CUR = "thread-CUR"
_OTHER = "thread-OTHER"


@pytest.fixture()
def workspace(tmp_path, monkeypatch):
    from tools import sandbox_config

    ws = tmp_path / "ws"
    proj = ws / "projects" / "p"

    files = {
        proj / "shared_doc.md": "shared NEEDLE here",
        proj / ".threads" / _CUR / "working" / "mine.md": "my own NEEDLE",
        proj / ".threads" / _CUR / "context" / "own_ctx.md": "durable NEEDLE",
        proj / ".threads" / _OTHER / "working" / "theirs.md": "their NEEDLE",
        proj / ".threads" / _OTHER / "context" / "their_ctx.md": "their durable NEEDLE",
        proj / ".threads" / _OTHER / "PLAN.md": "their plan NEEDLE",
        ws / ".gitignore": ".threads/\nprojects/*/.threads/\n",
    }
    for path, text in files.items():
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding="utf-8")
    monkeypatch.setattr(sandbox_config, "WORKSPACE", ws)
    monkeypatch.setattr(sandbox_config, "_STATIC_ALLOWED_RW", [ws])
    monkeypatch.setattr(sandbox_config, "_STATIC_ALLOWED_RO", [])
    monkeypatch.setattr(
        sandbox_config,
        "_list_authoritative_repo_policies",
        lambda: [],
    )
    return ws


def _rel_set(output: str) -> set[str]:
    if output.startswith("No matches"):
        return set()
    return {line.strip() for line in output.splitlines() if line.strip()}


async def _grep(ws, *, all_threads=False, session=_CUR):
    return _rel_set(
        await GrepTool(workspace=ws).execute(
            pattern="NEEDLE", output_mode="files",
            all_threads=all_threads, _thread_id=session,
        )
    )


async def _glob(ws, *, all_threads=False, session=_CUR):
    return _rel_set(
        await GlobTool(workspace=ws).execute(
            pattern="**/*.md", all_threads=all_threads, _thread_id=session,
        )
    )


_OTHER_WORKING = f"projects/p/.threads/{_OTHER}/working/theirs.md"
_OWN_WORKING = f"projects/p/.threads/{_CUR}/working/mine.md"
_OWN_CONTEXT = f"projects/p/.threads/{_CUR}/context/own_ctx.md"
_OTHER_CONTEXT = f"projects/p/.threads/{_OTHER}/context/their_ctx.md"
_OTHER_PLAN = f"projects/p/.threads/{_OTHER}/PLAN.md"
_SHARED = "projects/p/shared_doc.md"
_OTHERS = {_OTHER_WORKING, _OTHER_CONTEXT, _OTHER_PLAN}


@pytest.mark.parametrize("ripgrep", [True, False])
async def test_grep_default_searches_own_thread_folder_but_no_other(workspace, monkeypatch, ripgrep):
    import tools.grep as grep_mod

    if ripgrep and not grep_mod._RG_AVAILABLE:
        pytest.skip("ripgrep not available")
    monkeypatch.setattr(grep_mod, "_RG_AVAILABLE", ripgrep)
    hits = await _grep(workspace)
    assert {_OWN_WORKING, _OWN_CONTEXT, _SHARED} <= hits
    assert not hits & _OTHERS


@pytest.mark.parametrize("ripgrep", [True, False])
async def test_grep_all_threads_includes_every_thread_folder(workspace, monkeypatch, ripgrep):
    import tools.grep as grep_mod

    if ripgrep and not grep_mod._RG_AVAILABLE:
        pytest.skip("ripgrep not available")
    monkeypatch.setattr(grep_mod, "_RG_AVAILABLE", ripgrep)
    hits = await _grep(workspace, all_threads=True)
    assert {_OWN_WORKING, _OWN_CONTEXT, _SHARED} | _OTHERS <= hits


async def test_glob_default_searches_own_thread_folder_but_no_other(workspace):
    hits = await _glob(workspace)
    assert {_OWN_WORKING, _OWN_CONTEXT, _SHARED} <= hits
    assert not hits & _OTHERS


async def test_glob_all_threads_includes_every_thread_folder(workspace):
    hits = await _glob(workspace, all_threads=True)
    assert {_OWN_WORKING, _OWN_CONTEXT, _SHARED} | _OTHERS <= hits


async def test_grep_content_context_no_stray_separators_when_all_filtered(workspace):
    """content mode + context>0: when a group's only real lines live in another
    thread's working tray, the group-separator ('--') remainder must not leak as
    output — the tool returns 'No matches', not stray separators."""
    import tools.grep as grep_mod

    if not grep_mod._RG_AVAILABLE:
        import pytest as _pytest
        _pytest.skip("ripgrep not available; '--' separators are rg-specific")

    ws = workspace
    only = ws / "projects" / "p" / ".threads" / _OTHER / "working" / "solo.md"
    only.parent.mkdir(parents=True, exist_ok=True)
    # Two matches → ripgrep emits a "--" group separator between the context
    # blocks; both blocks live under the other thread's working tray.
    only.write_text(
        "SOLO one\nfiller\nfiller\nfiller\nSOLO two\n", encoding="utf-8"
    )

    out = await GrepTool(workspace=ws).execute(
        pattern="SOLO", output_mode="content", context=1, _thread_id=_CUR,
    )
    assert out == "No matches for pattern 'SOLO'"
    assert "--" not in out


@pytest.mark.parametrize("output_mode", ["content", "count"])
async def test_grep_direct_other_thread_file_isolated_with_ripgrep(
    workspace,
    output_mode,
):
    import tools.grep as grep_mod

    if not grep_mod._RG_AVAILABLE:
        pytest.skip("ripgrep not available; direct-file output is rg-specific")

    target = (
        workspace
        / "projects"
        / "p"
        / ".threads"
        / _OTHER
        / "working"
        / "theirs.md"
    )

    result = await GrepTool(workspace=workspace).execute(
        pattern="NEEDLE",
        path=str(target),
        output_mode=output_mode,
        _thread_id=_CUR,
    )

    assert result == f"No matches for pattern 'NEEDLE'\n{THREADS_NOTICE}"


async def test_no_session_isolates_every_thread_folder(workspace):
    # With no current-thread id, every thread folder is "other": the conservative default.
    hits = await GrepTool(workspace=workspace).execute(
        pattern="NEEDLE", output_mode="files", _thread_id=None,
    )
    rels = _rel_set(hits)
    assert not rels & (_OTHERS | {_OWN_WORKING, _OWN_CONTEXT})
    assert _SHARED in rels


async def test_search_tools_can_cross_projects_inside_workspace(workspace):
    sibling = workspace / "projects" / "sibling"
    target = sibling / "shared" / "cross-project.md"
    target.parent.mkdir(parents=True)
    target.write_text("CROSS_PROJECT_NEEDLE\n", encoding="utf-8")
    project_root = workspace / "projects" / "p"

    grep = await GrepTool(workspace=project_root).execute(
        pattern="CROSS_PROJECT_NEEDLE",
        path=str(sibling),
        output_mode="files",
        _thread_id=_CUR,
    )
    glob = await GlobTool(workspace=project_root).execute(
        pattern="**/*.md",
        path=str(sibling),
        _thread_id=_CUR,
    )

    assert "shared/cross-project.md" in _rel_set(grep)
    assert "shared/cross-project.md" in _rel_set(glob)


async def test_search_tools_can_read_registered_external_repo(
    workspace, monkeypatch, tmp_path
):
    from infra.project_repo_policy import RepoPolicy
    from tools import sandbox_config

    repo = tmp_path / "external" / "repo"
    target = repo / "docs" / "external.md"
    target.parent.mkdir(parents=True)
    target.write_text("EXTERNAL_NEEDLE\n", encoding="utf-8")
    monkeypatch.setattr(
        sandbox_config,
        "_list_authoritative_repo_policies",
        lambda: [RepoPolicy("p2", "external", repo.resolve(), "main", True)],
    )

    grep = await GrepTool(workspace=workspace).execute(
        pattern="EXTERNAL_NEEDLE",
        path=str(repo),
        output_mode="files",
        _thread_id=_CUR,
    )
    glob = await GlobTool(workspace=workspace).execute(
        pattern="**/*.md",
        path=str(repo),
        _thread_id=_CUR,
    )

    assert "docs/external.md" in _rel_set(grep)
    assert "docs/external.md" in _rel_set(glob)


async def test_search_tools_reject_paths_outside_workspace_and_registered_repos(
    workspace, monkeypatch, tmp_path
):
    from tools import sandbox_config

    outside = tmp_path / "outside"
    target = outside / "secret.md"
    outside.mkdir()
    target.write_text("OUTSIDE_NEEDLE\n", encoding="utf-8")
    monkeypatch.setattr(sandbox_config, "is_safehouse_available", lambda: False)

    grep = await GrepTool(workspace=workspace).execute(
        pattern="OUTSIDE_NEEDLE",
        path=str(outside),
        output_mode="files",
        _thread_id=_CUR,
    )
    glob = await GlobTool(workspace=workspace).execute(
        pattern="**/*.md",
        path=str(outside),
        _thread_id=_CUR,
    )

    assert grep.startswith("Error: Path")
    assert "outside the sandbox" in grep
    assert glob.startswith("Error: Path")
    assert "outside the sandbox" in glob


async def test_search_tools_exclude_symlinks_to_unregistered_outside_files(
    workspace, monkeypatch, tmp_path
):
    outside = tmp_path / "outside-secret.md"
    outside.write_text("SYMLINK_ESCAPE_NEEDLE\n", encoding="utf-8")
    link = workspace / "projects" / "p" / "shared" / "outside-link.md"
    link.parent.mkdir(parents=True)
    link.symlink_to(outside)
    monkeypatch.setattr("tools.grep._RG_AVAILABLE", False)

    grep = await GrepTool(workspace=workspace).execute(
        pattern="SYMLINK_ESCAPE_NEEDLE",
        output_mode="files",
        _thread_id=_CUR,
    )
    glob = await GlobTool(workspace=workspace).execute(
        pattern="**/*.md",
        _thread_id=_CUR,
    )

    assert "projects/p/shared/outside-link.md" not in _rel_set(grep)
    assert "projects/p/shared/outside-link.md" not in _rel_set(glob)


async def test_search_alias_to_other_thread_working_respects_isolation(
    workspace, monkeypatch
):
    target = (
        workspace
        / "projects"
        / "p"
        / ".threads"
        / _OTHER
        / "working"
        / "theirs.md"
    )
    alias = workspace / "projects" / "p" / "shared" / "theirs-link.md"
    alias.parent.mkdir(parents=True, exist_ok=True)
    alias.symlink_to(target)
    monkeypatch.setattr("tools.grep._RG_AVAILABLE", False)
    alias_rel = "projects/p/shared/theirs-link.md"

    default_grep = await _grep(workspace)
    default_glob = await _glob(workspace)
    all_grep = await _grep(workspace, all_threads=True)
    all_glob = await _glob(workspace, all_threads=True)

    assert alias_rel not in default_grep
    assert alias_rel not in default_glob
    assert alias_rel in all_grep
    assert alias_rel in all_glob


async def test_ripgrep_argv_disables_config_follow_and_separates_pattern_path(
    workspace, monkeypatch
):
    commands: list[tuple[str, ...]] = []

    class _Process:
        returncode = 0

        async def communicate(self):
            return b"", b""

    async def _capture(*args, **_kwargs):
        commands.append(args)
        return _Process()

    monkeypatch.setattr("tools.grep._RG_AVAILABLE", True)
    monkeypatch.setattr(asyncio, "create_subprocess_exec", _capture)

    result = await GrepTool(workspace=workspace).execute(
        pattern="--follow",
        output_mode="files",
        _thread_id=_CUR,
    )

    assert result == "No matches for pattern '--follow'"
    listing, search = commands
    for argv in commands:
        assert {"--no-config", "--no-follow", "--hidden"} <= set(argv)
    assert "--files" in listing and f"**/.threads/{_CUR}/**" in listing
    globs = [search[index + 1] for index, arg in enumerate(search) if arg == "--glob"]
    # No including glob, so ripgrep keeps honoring .gitignore; the walk never
    # enters a .threads folder, which the listing hands over as a root instead.
    assert not [glob for glob in globs if not glob.startswith("!")]
    assert globs[-1] == "!.threads/"
    assert search[-4:] == ("-e", "--follow", "--", str(workspace.resolve()))
