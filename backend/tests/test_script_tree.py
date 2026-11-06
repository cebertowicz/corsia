"""An authenticated script's approval covers every file its run can load from its folder."""
from __future__ import annotations

import hashlib
import os
import py_compile
import subprocess
import sys
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from automation import script_access, script_tree
from automation.script_sandbox import script_command
from host_access.review_models import ReviewerIdentity, ScriptApplicationReview


def test_tree_holds_every_file_but_bytecode_caches(tmp_path):
    entry = tmp_path / "run.py"
    entry.write_text("import helper\n")
    (tmp_path / "helper.py").write_text("X = 1\n")
    (tmp_path / "pkg").mkdir()
    (tmp_path / "pkg" / "mod.py").write_text("Y = 2\n")
    (tmp_path / "__pycache__").mkdir()
    (tmp_path / "__pycache__" / "helper.cpython-313.pyc").write_bytes(b"\0cache")

    tree = script_tree.read_tree(entry)

    assert tree == {"run.py": b"import helper\n", "helper.py": b"X = 1\n", "pkg/mod.py": b"Y = 2\n"}
    (tmp_path / "pkg" / "mod.py").write_text("Y = 3\n")
    assert script_tree.tree_sha256(script_tree.read_tree(entry)) != script_tree.tree_sha256(tree)


@pytest.mark.parametrize("make", ["folder_link", "file_link", "too_large"])
def test_tree_refuses_what_it_cannot_cover_and_names_the_fix(tmp_path, make):
    entry = tmp_path / "run.py"
    entry.write_text("pass\n")
    if make == "folder_link":
        target = tmp_path.parent / f"{tmp_path.name}-pkg"
        target.mkdir()
        (tmp_path / "pkg").symlink_to(target)
        expected = "copy what the script loads"
    elif make == "file_link":  # what it points at may be outside what the run or the agent can read
        outside = tmp_path.parent / f"{tmp_path.name}-outside.py"
        outside.write_text("Z = 3\n")
        (tmp_path / "linked.py").symlink_to(outside)
        expected = "linked.py in the script's folder is a link or another kind of special file"
    else:
        (tmp_path / "data.bin").write_bytes(b"\0" * 1_000_001)
        expected = "folder of its own"
    with pytest.raises(ValueError, match=expected):
        script_tree.read_tree(entry)


def test_tree_hash_is_canonical():
    assert script_tree.tree_sha256({"a": b"1", "b": b"2"}) == script_tree.tree_sha256({"b": b"2", "a": b"1"})
    assert script_tree.tree_sha256({"a": b"1", "b": b"2"}) != script_tree.tree_sha256({"a": b"12"})


@pytest.mark.asyncio
async def test_review_shows_the_folder_and_records_its_hash(walled):
    ws, review, reviewer = walled
    entry = ws / "tools" / "run.py"
    entry.parent.mkdir()
    entry.write_text("import helper\n")
    (entry.parent / "helper.py").write_text("X = 1\n")
    (entry.parent / "native.so").write_bytes(b"\xff\xfe\0")

    _, _, record = await review(entry)

    application = reviewer.await_args.args[1]
    assert application["script"] == "import helper\n"
    assert sorted(application["files"], key=lambda item: item["path"]) == [
        {"path": "helper.py", "text": "X = 1\n"},
        {"path": "native.so", "size": 3, "binary": True},
    ]
    assert record["tree_sha256"] == script_tree.tree_sha256(script_tree.read_tree(entry))


def test_authenticated_python_never_loads_bytecode_from_the_folder(tmp_path):
    """A .pyc planted beside reviewed source, stamped to match it, still never runs."""
    helper = tmp_path / "helper.py"
    helper.write_text("print('pyc')\n")
    py_compile.compile(str(helper), cfile=None, doraise=True)
    stamp = helper.stat().st_mtime
    helper.write_text("print('src')\n")  # same size, so only the stamp tells them apart
    os.utime(helper, (stamp, stamp))
    entry = tmp_path / "run.py"
    entry.write_text("import helper\n")
    python = os.path.dirname(sys.executable)
    env = {**os.environ, "PATH": f"{python}:{os.environ.get('PATH', '')}"}
    env.pop("PYTHONPYCACHEPREFIX", None)
    env.pop("PYTHONDONTWRITEBYTECODE", None)

    def run(**kwargs) -> bytes:
        return subprocess.run(["bash", "-c", script_command(entry, **kwargs)], cwd=tmp_path, env=env,
                              capture_output=True, timeout=30, check=True).stdout

    assert run() == b"pyc\n"  # the hole: an unconfined run loads the planted bytecode
    assert run(pycache_dir=tmp_path.parent / f"{tmp_path.name}-pycache") == b"src\n"


@pytest.fixture
def walled(tmp_path, monkeypatch):
    """A workspace with a project the requester is walled from, and a reviewer that never answers."""
    from tools import sandbox_config
    from tools.project_fence import Fence, FencedRoot

    ws = tmp_path / "workspace"
    private = ws / "projects" / "Private"
    private.mkdir(parents=True)
    (private / "secret.txt").write_text("walled\n")
    monkeypatch.setattr(script_access, "fence_for_thread", lambda _thread_id: Fence((FencedRoot(private),)))
    monkeypatch.setattr(sandbox_config, "WORKSPACE", ws)
    monkeypatch.setattr("infra.config.WORKSPACE", ws)
    monkeypatch.setattr(script_access, "WORKSPACE", ws)
    monkeypatch.setattr(sandbox_config, "_STATIC_ALLOWED_RW", [ws])
    monkeypatch.setattr(sandbox_config, "_STATIC_ALLOWED_RO", [])
    monkeypatch.setattr(sandbox_config, "_list_authoritative_repo_policies", lambda: [])
    monkeypatch.setattr(script_access, "get_host_access_auto_approve", lambda: True)
    monkeypatch.setattr(script_access.reviewer_target, "resolve_reviewer_target",
                        AsyncMock(return_value=SimpleNamespace(harness="native")))
    identity = ReviewerIdentity("test", "native", "test", "test", "test")
    reviewer = AsyncMock(return_value=ScriptApplicationReview(identity, "allow", "ok"))
    monkeypatch.setattr(script_access, "review_application", reviewer)

    async def review(entry):
        entry.parent.mkdir(parents=True, exist_ok=True)
        if not entry.exists():
            entry.write_text("pass\n")
        sha = hashlib.sha256(entry.read_bytes()).hexdigest()
        monkeypatch.setattr(script_access, "pin_script", lambda *args, **kwargs: (entry, sha))
        return await script_access.review_script(
            "requester", {"script_path": str(entry), "purpose": "work", "user_request": "please"})

    return ws, review, reviewer


@pytest.mark.asyncio
@pytest.mark.parametrize("where", ["holds_a_walled_project", "in_a_walled_project", "workspace_git"])
async def test_review_reads_nothing_the_requester_cannot_read(walled, where):
    ws, review, reviewer = walled
    entry = {"holds_a_walled_project": ws / "projects" / "x.py",
             "in_a_walled_project": ws / "projects" / "Private" / "run.py",
             "workspace_git": ws / ".git" / "logs" / "HEAD"}[where]
    with pytest.raises(ValueError):
        await review(entry)
    reviewer.assert_not_awaited()


@pytest.mark.asyncio
async def test_a_file_swapped_for_a_link_after_its_check_is_never_read(walled, monkeypatch):
    ws, review, reviewer = walled
    entry = ws / "tools" / "run.py"
    entry.parent.mkdir()
    entry.write_text("import helper\n")
    helper = entry.parent / "helper.py"
    helper.write_text("X = 1\n")
    outside = ws.parent / "outside-secret.txt"
    outside.write_text("outside-bytes\n")
    checked = script_access._read_denial

    def swap_after_check(path, *args):
        denial = checked(path, *args)
        if path == helper:
            helper.unlink()
            helper.symlink_to(outside)
        return denial

    monkeypatch.setattr(script_access, "_read_denial", swap_after_check)
    with pytest.raises(ValueError, match="helper.py in the script's folder is a link") as refused:
        await review(entry)
    assert "outside-bytes" not in str(refused.value)
    reviewer.assert_not_awaited()


def test_a_folder_reached_through_a_link_is_refused(tmp_path):
    real = tmp_path / "real"
    real.mkdir()
    (real / "run.py").write_text("pass\n")
    (tmp_path / "alias").symlink_to(real, target_is_directory=True)
    with pytest.raises(ValueError, match="without going through a link"):
        script_tree.read_tree(tmp_path / "alias" / "run.py")


@pytest.mark.parametrize("make", ["pipe", "unlistable_folder", "unreadable_file"])
def test_what_the_walk_cannot_read_refuses_the_folder_by_its_path(tmp_path, make):
    """A run could still load it by name, so leaving it out of the hash would let it change unreviewed."""
    entry = tmp_path / "run.py"
    entry.write_text("pass\n")
    (tmp_path / "lib").mkdir()
    if make == "pipe":
        os.mkfifo(tmp_path / "lib" / "x.sh")
        expected = "lib/x.sh in the script's folder is a link or another kind of special file"
    elif make == "unlistable_folder":
        (tmp_path / "lib" / "x.sh").write_text("echo hi\n")
        (tmp_path / "lib").chmod(0o300)
        expected = "lib in the script's folder can't be read; make it readable"
    else:
        (tmp_path / "lib" / "x.sh").write_text("echo hi\n")
        (tmp_path / "lib" / "x.sh").chmod(0)
        expected = "lib/x.sh in the script's folder can't be read; make it readable"
    try:
        with pytest.raises(ValueError, match=expected) as refused:
            script_tree.read_tree(entry)
    finally:
        (tmp_path / "lib").chmod(0o700)
    assert "Errno" not in str(refused.value)
