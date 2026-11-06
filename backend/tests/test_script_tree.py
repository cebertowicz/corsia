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
        expected = "linked.py in the script's folder is a link; copy what the script loads"
    else:
        (tmp_path / "data.bin").write_bytes(b"\0" * 1_000_001)
        expected = "folder of its own"
    with pytest.raises(ValueError, match=expected):
        script_tree.read_tree(entry)


def test_tree_hash_is_canonical():
    assert script_tree.tree_sha256({"a": b"1", "b": b"2"}) == script_tree.tree_sha256({"b": b"2", "a": b"1"})
    assert script_tree.tree_sha256({"a": b"1", "b": b"2"}) != script_tree.tree_sha256({"a": b"12"})


@pytest.mark.asyncio
async def test_review_shows_the_folder_and_records_its_hash(monkeypatch, tmp_path):
    entry = tmp_path / "run.py"
    entry.write_text("import helper\n")
    (tmp_path / "helper.py").write_text("X = 1\n")
    (tmp_path / "native.so").write_bytes(b"\xff\xfe\0")
    sha = hashlib.sha256(entry.read_bytes()).hexdigest()
    monkeypatch.setattr(script_access, "pin_script", lambda *args, **kwargs: (entry, sha))
    monkeypatch.setattr(script_access, "get_host_access_auto_approve", lambda: True)
    monkeypatch.setattr(script_access.reviewer_target, "resolve_reviewer_target",
                        AsyncMock(return_value=SimpleNamespace(harness="native")))
    identity = ReviewerIdentity("test", "native", "test", "test", "test")
    reviewer = AsyncMock(return_value=ScriptApplicationReview(identity, "allow", "ok"))
    monkeypatch.setattr(script_access, "review_application", reviewer)

    _, _, record = await script_access.review_script(
        "owner", {"script_path": str(entry), "purpose": "work", "user_request": "please"},
    )

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
