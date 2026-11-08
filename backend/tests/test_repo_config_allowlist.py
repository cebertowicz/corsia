"""Tests for the repository Git-config allowlist an agent registration must pass.

A repository's own Git config can name a program, and the backend runs Git inside a
registered checkout outside the agent sandbox, so an agent must not be able to attach
a repository whose config is wider than the allowlist. These cover the check itself
against real on-disk repositories and Git's own key names, the two agent registration
routes that call it, and the person's Project-settings route that deliberately does
not. The negative route tests also prove the program a refused key names never ran.
"""

from __future__ import annotations

import os
import sqlite3
import subprocess
from pathlib import Path

import pytest

from infra.repo_config_allowlist import (
    refused_repo_config_keys,
    repo_config_refusal,
)
from tests.github_screen_schema import SCREEN_INSTALL_SCHEMA_SQL

PROGRAM = "/opt/never-run/sentinel-program"
ALLOWED_CONFIG = """\
[core]
\trepositoryformatversion = 0
\tfilemode = true
\tbare = false
\tlogallrefupdates = true
\tignorecase = true
\tprecomposeunicode = true
[user]
\tname = Someone
\temail = someone@example.invalid
\t signingkey = ABCDEF0123456789
[remote "origin"]
\turl = example-host:team/app.git
\tfetch = +refs/heads/*:refs/remotes/origin/*
\tpushurl = example-host:team/app.git
\ttagOpt = --no-tags
[BRANCH "release/nested.sub.section"]
\tREMOTE = origin
\tmerge = refs/heads/release/nested.sub.section
"""

# (config text, the key name Git reports for it)
REFUSED_CONFIGS = [
    (f"[core]\n\tfsmonitor = {PROGRAM}\n", "core.fsmonitor"),
    (f'[filter "x"]\n\tclean = {PROGRAM}\n', "filter.x.clean"),
    (f"[core]\n\tsshCommand = {PROGRAM}\n", "core.sshcommand"),
    (f"[core]\n\tpager = {PROGRAM}\n", "core.pager"),
    (f'[diff "bin"]\n\ttextconv = {PROGRAM}\n', "diff.bin.textconv"),
    ("[include]\n\tpath = ./other-config\n", "include.path"),
    ('[includeIf "gitdir:/x"]\n\tpath = ./other-config\n', "includeif.gitdir:/x.path"),
    ("[core]\n\tsymlinks = false\n", "core.symlinks"),
    ("[credential]\n\thelper = store\n", "credential.helper"),
    ('[core "qualified"]\n\tbare = false\n', "core.qualified.bare"),
    ('[url "example-host:other.git"]\n\tpushInsteadOf = example-host:app.git\n',
     "url.example-host:other.git.pushinsteadof"),
]


def _git_repo(path: Path) -> None:
    path.mkdir(parents=True, exist_ok=True)
    subprocess.run(
        ["git", "-c", "init.defaultRefFormat=files", "init", "-b", "main", str(path)],
        check=True, capture_output=True, text=True,
    )
    (path / "tracked.txt").write_text("base\n", encoding="utf-8")
    subprocess.run(
        ["git", "-C", str(path), "add", "tracked.txt"],
        check=True, capture_output=True, text=True,
    )
    subprocess.run(
        ["git", "-C", str(path), "-c", "user.name=CORSIA Test",
         "-c", "user.email=test@corsia.local", "commit", "-m", "base"],
        check=True, capture_output=True, text=True,
    )


def _config_file(repo: Path) -> Path:
    return repo / ".git" / "config"


def _write_config(repo: Path, text: str) -> None:
    """Replace a repository's own config with ``text``."""
    _config_file(repo).write_text(text, encoding="utf-8")


def _append_config(repo: Path, text: str) -> None:
    with _config_file(repo).open("a", encoding="utf-8") as handle:
        handle.write(text)


def _fsmonitor_marker(repo: Path) -> Path:
    """Point ``core.fsmonitor`` at a program that records that it ran.

    Git runs this hook when it reads the index in that repository, which is exactly
    what a backend Git call would do after registration; the tests then require that
    the record was never written.
    """
    marker = repo.parent / "program-ran"
    hook = repo.parent / "fsmonitor-hook.sh"
    hook.write_text(
        f"#!/bin/sh\nprintf x > '{marker}'\nexit 1\n",
        encoding="utf-8",
    )
    os.chmod(hook, 0o755)
    _append_config(repo, f"[core]\n\tfsmonitor = {hook}\n")
    return marker


def _policy_db() -> sqlite3.Connection:
    db = sqlite3.connect(":memory:", timeout=2, check_same_thread=False)
    db.row_factory = sqlite3.Row
    db.executescript(SCREEN_INSTALL_SCHEMA_SQL)
    db.executescript(
        """
        CREATE TABLE projects (
            id TEXT PRIMARY KEY,
            name,
            path,
            doc_roots,
            conductor_thread_id
        );
        CREATE TABLE IF NOT EXISTS project_repo_policies (
            project_id TEXT NOT NULL,
            repo_name TEXT NOT NULL,
            repo_path TEXT NOT NULL,
            pinned_branch TEXT,
            protect_base_checkout INTEGER NOT NULL,
            PRIMARY KEY (project_id, repo_name)
        )
        """
    )
    db.execute(
        "INSERT INTO projects (id, name, path, doc_roots) "
        "VALUES ('p1', 'Demo', '/tmp/demo', '[]')"
    )
    db.commit()
    return db


class _NoClose:
    def __init__(self, db: sqlite3.Connection):
        self._db = db

    def __getattr__(self, name):
        return getattr(self._db, name)

    def close(self) -> None:
        pass


async def _run_now(callback):
    return callback()


@pytest.fixture
def wired(monkeypatch, tmp_path):
    """Route the shared add-repo procedure at an isolated workspace and database.

    Only the process-wide seams move (home, workspace dir, DB handle, DB thread), so
    the registration procedure itself runs for real on both the agent and the person
    route.
    """
    from api import project_repos

    monkeypatch.setattr(Path, "home", classmethod(lambda cls: tmp_path))
    workspace = tmp_path / "projects" / "demo"
    workspace.mkdir(parents=True, exist_ok=True)
    db = _policy_db()
    monkeypatch.setattr(project_repos, "_workspace_dir_for", lambda _id: workspace)
    monkeypatch.setattr(project_repos, "get_db", lambda: _NoClose(db))
    monkeypatch.setattr("infra.db_async.run_in_db", _run_now, raising=False)
    return {"db": db, "workspace": workspace, "tmp": tmp_path}


# --- the check itself -------------------------------------------------------


def test_a_repo_using_only_allowed_keys_passes(tmp_path) -> None:
    repo = tmp_path / "clean"
    _git_repo(repo)
    _write_config(repo, ALLOWED_CONFIG)

    assert repo_config_refusal(repo) is None


@pytest.mark.parametrize(
    ("text", "expected_key"),
    REFUSED_CONFIGS,
    ids=[expected for _, expected in REFUSED_CONFIGS],
)
def test_a_key_outside_the_allowlist_is_refused_by_name(tmp_path, text, expected_key) -> None:
    repo = tmp_path / "widened"
    _git_repo(repo)
    _append_config(repo, text)

    refusal = repo_config_refusal(repo)

    assert refusal is not None
    assert expected_key in refusal, refusal
    assert "Project settings" in refusal, refusal


def test_the_refusal_names_keys_and_never_values(tmp_path) -> None:
    repo = tmp_path / "values"
    _git_repo(repo)
    _append_config(repo, f"[core]\n\tsshCommand = {PROGRAM}\n\tfsmonitor = {PROGRAM}\n")

    refusal = repo_config_refusal(repo)

    assert refusal is not None and PROGRAM not in refusal, refusal


def test_refused_keys_come_back_in_full_and_the_agent_message_caps(tmp_path) -> None:
    repo = tmp_path / "many"
    _git_repo(repo)
    _append_config(
        repo,
        "[core]\n\tsymlinks = false\n\tpager = less\n[filter \"a\"]\n\tclean = cat\n"
        "[filter \"b\"]\n\tclean = cat\n[filter \"c\"]\n\tclean = cat\n",
    )

    refusal = repo_config_refusal(repo)

    assert refused_repo_config_keys(repo) == [
        "core.pager", "core.symlinks", "filter.a.clean",
        "filter.b.clean", "filter.c.clean",
    ]
    assert refusal is not None and "and 2 more" in refusal, refusal
    assert "filter.c.clean" not in refusal, refusal


def test_a_key_only_in_config_worktree_is_refused(tmp_path) -> None:
    repo = tmp_path / "worktree-config"
    _git_repo(repo)
    (repo / ".git" / "config.worktree").write_text(
        f'[filter "wt"]\n\tclean = {PROGRAM}\n', encoding="utf-8"
    )

    refusal = repo_config_refusal(repo)

    assert refusal is not None and "filter.wt.clean" in refusal, refusal


def test_a_linked_worktree_is_checked_through_its_common_config(tmp_path) -> None:
    repo = tmp_path / "common"
    _git_repo(repo)
    linked = tmp_path / "linked"
    subprocess.run(
        ["git", "-C", str(repo), "worktree", "add", "-b", "wt", str(linked)],
        check=True, capture_output=True, text=True,
    )
    assert (linked / ".git").is_file()
    _append_config(repo, f"[filter \"lfs\"]\n\tprocess = {PROGRAM}\n")

    refusal = repo_config_refusal(linked)

    assert refusal is not None and "filter.lfs.process" in refusal, refusal


def test_a_tilde_pointer_is_read_as_literally_as_git_reads_it(tmp_path, monkeypatch) -> None:
    # A clean Git directory where ``~`` would expand to, and a widened one where Git
    # actually looks: ``~/real`` relative to the checkout.
    home = tmp_path / "home"
    home.mkdir()
    _git_repo(tmp_path / "clean")
    (tmp_path / "clean" / ".git").rename(home / "real")
    monkeypatch.setenv("HOME", str(home))

    repo = tmp_path / "pointed"
    (repo / "~").mkdir(parents=True)
    _git_repo(tmp_path / "widened")
    (tmp_path / "widened" / ".git").rename(repo / "~" / "real")
    with (repo / "~" / "real" / "config").open("a", encoding="utf-8") as handle:
        handle.write(f'[filter "x"]\n\tclean = {PROGRAM}\n')
    (repo / ".git").write_text("gitdir: ~/real\n", encoding="utf-8")
    git_dir = subprocess.run(
        ["git", "-C", str(repo), "rev-parse", "--absolute-git-dir"],
        check=True, capture_output=True, text=True,
    ).stdout.strip()
    assert Path(git_dir) == (repo / "~" / "real").resolve()

    refusal = repo_config_refusal(repo)

    assert refusal is not None and "filter.x.clean" in refusal, refusal


def _clean_and_widened_git_dirs(tmp_path: Path, clean: Path, widened: Path) -> None:
    """Put a clean Git directory where a lax parser would look, a widened one where Git does."""
    for name, target in (("clean-src", clean), ("widened-src", widened)):
        _git_repo(tmp_path / name)
        target.parent.mkdir(parents=True, exist_ok=True)
        (tmp_path / name / ".git").rename(target)
    with (widened / "config").open("a", encoding="utf-8") as handle:
        handle.write(f'[filter "x"]\n\tclean = {PROGRAM}\n')


@pytest.mark.parametrize("pointer, clean_name, widened_name", [
    ("gitdir:  real\n", "real", " real"),
    ("gitdir: real \n", "real", "real "),
])
def test_spaces_in_a_gitdir_pointer_are_kept_as_git_keeps_them(
    tmp_path, pointer, clean_name, widened_name,
) -> None:
    repo = tmp_path / "pointed"
    _clean_and_widened_git_dirs(tmp_path, repo / clean_name, repo / widened_name)
    (repo / ".git").write_text(pointer, encoding="utf-8")
    git_dir = subprocess.run(
        ["git", "-C", str(repo), "rev-parse", "--absolute-git-dir"],
        check=True, capture_output=True, text=True,
    ).stdout.rstrip("\n")
    assert Path(git_dir) == (repo / widened_name).resolve()

    refusal = repo_config_refusal(repo)

    assert refusal is not None and "filter.x.clean" in refusal, refusal


def test_a_gitdir_pointer_git_refuses_is_refused(tmp_path) -> None:
    repo = tmp_path / "no-space"
    _clean_and_widened_git_dirs(tmp_path, repo / "real", tmp_path / "unused")
    (repo / ".git").write_text(f"gitdir:{repo / 'real'}\n", encoding="utf-8")

    assert repo_config_refusal(repo) is not None


def test_a_commondir_is_read_literally_with_no_gitdir_prefix(tmp_path) -> None:
    repo = tmp_path / "common"
    _git_repo(repo)
    linked = tmp_path / "linked"
    subprocess.run(
        ["git", "-C", str(repo), "worktree", "add", "-b", "wt", str(linked)],
        check=True, capture_output=True, text=True,
    )
    worktree_git_dir = repo / ".git" / "worktrees" / "linked"
    _clean_and_widened_git_dirs(tmp_path, tmp_path / "shared", tmp_path / "shared ")
    (worktree_git_dir / "commondir").write_text(f"{tmp_path / 'shared'} \n", encoding="utf-8")
    common = subprocess.run(
        ["git", "-C", str(linked), "rev-parse", "--git-common-dir"],
        check=True, capture_output=True, text=True,
    ).stdout.rstrip("\n")
    assert Path(common).resolve() == (tmp_path / "shared ").resolve()

    refusal = repo_config_refusal(linked)

    assert refusal is not None and "filter.x.clean" in refusal, refusal


def test_a_credential_inside_a_key_name_is_masked(tmp_path) -> None:
    secret = "s3cr3t-value"
    repo = tmp_path / "credential-key"
    _git_repo(repo)
    _append_config(
        repo,
        f'[url "https://someone:{secret}@example.invalid/"]\n'
        "\tinsteadOf = https://example.invalid/\n",
    )

    refusal = repo_config_refusal(repo)
    keys = refused_repo_config_keys(repo)

    assert refusal is not None and "insteadof" in refusal, refusal
    assert secret not in refusal, refusal
    assert keys and not any(secret in key for key in keys), keys


@pytest.mark.parametrize("broken", ["unreadable", "unparsable", "missing"])
def test_a_config_that_cannot_be_checked_is_refused(tmp_path, broken) -> None:
    repo = tmp_path / f"broken-{broken}"
    _git_repo(repo)
    config = _config_file(repo)
    if broken == "unreadable":
        os.chmod(config, 0o000)
    elif broken == "unparsable":
        config.write_text("[core\n\tfilemode = true\n", encoding="utf-8")
    else:
        config.unlink()

    try:
        refusal = repo_config_refusal(repo)
    finally:
        if broken == "unreadable":
            os.chmod(config, 0o644)

    assert refusal is not None and "could not be read or parsed" in refusal, refusal


def test_a_plain_directory_has_nothing_of_its_own_to_run(tmp_path) -> None:
    folder = tmp_path / "notes"
    folder.mkdir()

    assert repo_config_refusal(folder) is None
    assert repo_config_refusal(tmp_path / "does-not-exist") is None


# --- agent registration routes ---------------------------------------------


@pytest.mark.asyncio
async def test_repo_register_refuses_a_widened_config_and_registers_nothing(
    wired,
) -> None:
    from tools.project_repo_register import ProjectRepoRegisterTool

    repo = wired["tmp"] / "external" / "app"
    _git_repo(repo)
    marker = _fsmonitor_marker(repo)

    result = await ProjectRepoRegisterTool().execute(
        repo_path=str(repo), _thread_id="thread-1", _thread_project_id="p1",
    )

    assert result.startswith("Error:") and "core.fsmonitor" in result, result
    assert wired["db"].execute("SELECT COUNT(*) FROM project_repo_policies").fetchone()[0] == 0
    assert not (wired["workspace"] / "source" / "app").is_symlink()
    assert not marker.exists(), "the program the refused key names ran"


@pytest.mark.asyncio
async def test_project_create_refuses_a_widened_config_and_creates_nothing(
    tmp_path, monkeypatch
) -> None:
    from tools.projects import CreateProjectTool

    repo = tmp_path / "external" / "app"
    _git_repo(repo)
    marker = _fsmonitor_marker(repo)

    def _no_scaffolding(*_args, **_kwargs):
        raise AssertionError("project scaffolding must not start for a refused repo")

    monkeypatch.setattr("api.project_scaffolding.initialize_project", _no_scaffolding)

    result = await CreateProjectTool().execute(
        name="Demo", repo_path=str(repo), _thread_id="caller",
    )

    assert result.startswith("❌") and "core.fsmonitor" in result, result
    assert "Project settings" in result, result
    assert not marker.exists(), "the program the refused key names ran"


def test_the_fsmonitor_fixture_does_run_when_git_reads_that_repo(tmp_path) -> None:
    """Guards the two tests above: the refused key names a program that really runs.

    Without this the marker assertion would pass for any repository at all.
    """
    repo = tmp_path / "live" / "app"
    _git_repo(repo)
    marker = _fsmonitor_marker(repo)

    subprocess.run(
        ["git", "-C", str(repo), "status", "--porcelain"],
        check=False, capture_output=True, text=True,
    )

    assert marker.exists(), "this fixture cannot prove a program ran"


@pytest.mark.asyncio
async def test_person_settings_route_still_registers_a_refused_repo(
    wired, monkeypatch
) -> None:
    """Project settings is the person's own action and stays unchecked."""
    from api import project_repos
    from api.project_repos import RepoAdd

    repo = wired["tmp"] / "external" / "app"
    _git_repo(repo)
    _append_config(repo, f"[core]\n\tfsmonitor = {PROGRAM}\n")
    assert repo_config_refusal(repo) is not None

    monkeypatch.setattr(project_repos, "authenticate", lambda _token: {"id": "u-test"})
    monkeypatch.setattr(project_repos, "run_in_db", _run_now)

    added = await project_repos.add_repo(
        "session-token", "p1", RepoAdd(mode="link", path=str(repo)),
    )

    assert added["added"] is True
    assert added["repo"]["name"] == "app"
    row = wired["db"].execute(
        "SELECT * FROM project_repo_policies WHERE repo_name = 'app'"
    ).fetchone()
    assert row is not None and row["pinned_branch"] == "main"
    assert (wired["workspace"] / "source" / "app").is_symlink()


# --- the corsia_api route (the agent's in-process API surface) ---------------


def test_corsia_api_refuses_every_call_that_attaches_a_widened_repo(isolated_db) -> None:
    """corsia_api can attach a repository too, so it carries the same check.

    A link add and project creation carrying a repo path are the two calls that bind
    an existing local directory; the workspace path keeps the B14 rule from being the
    one that refuses here.
    """
    from infra.config import WORKSPACE
    from tools.corsia_api import ApiCallError, _scope

    repo = WORKSPACE / "projects" / "widened" / "app"
    _git_repo(repo)
    marker = _fsmonitor_marker(repo)

    with pytest.raises(ApiCallError, match="core.fsmonitor"):
        _scope("POST", "/api/projects/p/repos", {}, {"mode": "link", "path": str(repo)})
    with pytest.raises(ApiCallError, match="core.fsmonitor"):
        _scope("POST", "/api/projects", {}, {"name": "Demo", "repo_path": str(repo)})
    with pytest.raises(ApiCallError, match="core.fsmonitor"):
        _scope("POST", "/api/projects", {}, {"name": "Demo", "path": str(repo)})

    assert not marker.exists(), "the program the refused key names ran"


def test_corsia_api_still_links_a_clean_repo_and_creates_a_new_one(isolated_db) -> None:
    from infra.config import WORKSPACE
    from tools.corsia_api import _scope

    clean = WORKSPACE / "projects" / "clean" / "app"
    _git_repo(clean)

    _scope("POST", "/api/projects/p/repos", {}, {"mode": "link", "path": str(clean)})
    _scope("POST", "/api/projects", {}, {"name": "Demo", "repo_path": str(clean)})
    _scope("POST", "/api/projects/p/repos", {}, {"mode": "create", "name": "fresh"})
    # A workspace path that holds no repository yet is this check's pass; the route
    # itself decides whether it can be attached.
    _scope("POST", "/api/projects/p/repos", {},
           {"mode": "link", "path": str(WORKSPACE / "projects" / "not-a-repo")})
