"""A shell with an agents' GitHub account pushes to GitHub over HTTPS, whatever Git rewrite the person has.

Real ``git`` resolves each push URL here; nothing touches a network.
"""
from __future__ import annotations

import os
import shutil
import subprocess

import pytest

from connections import github_agent
from connections.github_agent import AgentAccountView

pytestmark = pytest.mark.skipif(shutil.which("git") is None, reason="needs git")

GITHUB = "https://github.com/"
LOCKED = AgentAccountView("needs_auth", cause="signed_out")
READY = AgentAccountView("ready", home="/nonexistent/agent-home", binary="gh", login="corsia-agent", user_id=1)


@pytest.fixture
def git(tmp_path):
    home, work = tmp_path / "home", tmp_path / "work"
    home.mkdir()
    base = {"PATH": os.environ["PATH"], "HOME": str(home), "GIT_CONFIG_NOSYSTEM": "1"}

    def run(*args, env=None):
        return subprocess.run(["git", *args], cwd=work, env={**base, **(env or {})},
                              capture_output=True, text=True, check=True, timeout=30).stdout.strip()

    subprocess.run(["git", "init", "-q", str(work)], env=base, check=True, timeout=30)
    run("remote", "add", "https", f"{GITHUB}o/r.git")
    run("remote", "add", "scp", "git@github.com:o/r.git")
    run("remote", "add", "ssh", "ssh://git@github.com/o/r.git")
    run("remote", "add", "other", "git@git.example.com:o/r.git")
    return run


@pytest.mark.parametrize("view", [LOCKED, READY], ids=["locked", "ready"])
def test_github_pushes_go_over_https_even_when_the_persons_rewrite_points_at_ssh(git, view):
    git("config", "--global", "url.git@github.com:.insteadOf", GITHUB)
    assert git("remote", "get-url", "--push", "https") == "git@github.com:o/r.git"

    env = github_agent.shell_env(view)
    for remote in ("https", "scp", "ssh"):
        assert git("remote", "get-url", "--push", remote, env=env) == f"{GITHUB}o/r.git"


def test_an_ssh_pushurl_for_github_goes_over_https(git):
    git("config", "remote.https.pushurl", "git@github.com:o/r.git")

    assert git("remote", "get-url", "--push", "https", env=github_agent.shell_env(LOCKED)) == f"{GITHUB}o/r.git"


def test_other_hosts_and_fetches_keep_the_persons_urls(git):
    git("config", "--global", "url.git@github.com:.insteadOf", GITHUB)
    env = github_agent.shell_env(LOCKED)

    assert git("remote", "get-url", "--push", "other", env=env) == "git@git.example.com:o/r.git"
    assert git("remote", "get-url", "https", env=env) == "git@github.com:o/r.git"


@pytest.mark.parametrize("view", [LOCKED, READY], ids=["locked", "ready"])
def test_the_push_rule_is_in_the_names_safehouse_passes(view):
    from tools.shell_environment import shell_env_pass_names

    env = github_agent.shell_env(view)

    assert ("url.https://github.com/.pushInsteadOf", GITHUB) in [
        (env[f"GIT_CONFIG_KEY_{i}"], env[f"GIT_CONFIG_VALUE_{i}"]) for i in range(int(env["GIT_CONFIG_COUNT"]))
    ]
    assert set(env) <= set(shell_env_pass_names().split(","))
