"""A shell with an agents' GitHub account never pushes to GitHub over SSH as the person.

The person's own Git config may rewrite GitHub's HTTPS URLs to SSH. Real ``git`` runs
here against a fake ``ssh`` that only logs its arguments, so nothing touches a network.
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
def repo(tmp_path):
    home, bin_dir, work = tmp_path / "home", tmp_path / "bin", tmp_path / "work"
    for path in (home, bin_dir, work):
        path.mkdir()
    log = tmp_path / "ssh.log"
    fake = bin_dir / "ssh"
    # Answers ``-G`` like OpenSSH: the host alias ``work-gh`` resolves to GitHub, as an SSH config would.
    fake.write_text('#!/bin/sh\necho "$*" >> "$SSH_LOG"\n'
                    'if [ "$1" = -G ]; then for h; do :; done; case $h in *work-gh) h=GitHub.com;; esac; '
                    'echo "hostname ${h#*@}"; exit 0; fi\nexit 1\n')
    fake.chmod(0o755)
    base = {"PATH": f"{bin_dir}{os.pathsep}{os.environ['PATH']}", "HOME": str(home), "SSH_LOG": str(log),
            "GIT_CONFIG_NOSYSTEM": "1", "GIT_TERMINAL_PROMPT": "0"}

    def git(*args, env=None, check=True):
        return subprocess.run(["git", *args], cwd=work, env={**base, **(env or {})},
                              capture_output=True, text=True, check=check, timeout=30)

    git("init", "-q")
    git("-c", "user.name=t", "-c", "user.email=t@example.com", "commit", "-q", "--allow-empty", "-m", "x")
    git("remote", "add", "origin", f"{GITHUB}o/r.git")
    git("remote", "add", "other", "git@git.example.com:o/r.git")
    git("remote", "add", "alias", "git@work-gh:o/r.git")

    def ssh_calls():
        return log.read_text().splitlines() if log.exists() else []

    return git, ssh_calls


@pytest.mark.parametrize("view", [LOCKED, READY], ids=["locked", "ready"])
def test_the_persons_insteadof_rewrite_leaves_github_pushes_on_https(repo, view):
    git, _ = repo
    git("config", "--global", "url.git@github.com:.insteadOf", GITHUB)
    assert git("remote", "get-url", "--push", "origin").stdout.strip() == "git@github.com:o/r.git"

    env = github_agent.shell_env(view)
    assert git("remote", "get-url", "--push", "origin", env=env).stdout.strip() == f"{GITHUB}o/r.git"


@pytest.mark.parametrize("view", [LOCKED, READY], ids=["locked", "ready"])
def test_a_github_push_rewritten_to_ssh_is_refused_before_ssh_runs(repo, view):
    git, ssh_calls = repo
    # A push rewrite of the same length as CORSIA's own wins, so only the SSH command can stop it.
    git("config", "--global", "url.git@github.com:.pushInsteadOf", GITHUB)

    result = git("push", "origin", "HEAD:main", env=github_agent.shell_env(view), check=False)

    assert result.returncode != 0
    assert "agent shells push to GitHub over HTTPS" in result.stderr
    assert not [call for call in ssh_calls() if "git-receive-pack" in call]


def test_a_push_to_an_ssh_host_alias_for_github_is_refused(repo):
    git, ssh_calls = repo

    result = git("push", "alias", "HEAD:main", env=github_agent.shell_env(LOCKED), check=False)

    assert "agent shells push to GitHub over HTTPS" in result.stderr
    assert not [call for call in ssh_calls() if "git-receive-pack" in call]


def ran(calls, command):
    return any(call.endswith(command) for call in calls)


def test_ssh_fetches_and_other_hosts_run_as_before(repo):
    git, ssh_calls = repo
    git("config", "--global", "url.git@github.com:.insteadOf", GITHUB)
    env = github_agent.shell_env(LOCKED)

    git("ls-remote", "origin", env=env, check=False)
    git("push", "other", "HEAD:main", env=env, check=False)

    calls = ssh_calls()
    assert ran(calls, "git@github.com git-upload-pack 'o/r.git'")
    assert ran(calls, "git@git.example.com git-receive-pack 'o/r.git'")


def test_the_persons_own_ssh_command_still_runs(repo):
    git, ssh_calls = repo
    git("config", "core.sshCommand", "ssh -i /keys/work")

    git("push", "other", "HEAD:main", env=github_agent.shell_env(LOCKED), check=False)

    assert ran(ssh_calls(), "-i /keys/work git@git.example.com git-receive-pack 'o/r.git'")


def test_an_explicit_ssh_command_still_replaces_the_guard(repo):
    git, ssh_calls = repo
    git("config", "core.sshCommand", "ssh -i /keys/work")

    git("-c", "core.sshCommand=ssh -i /keys/cli", "push", "other", "HEAD:main",
        env=github_agent.shell_env(LOCKED), check=False)

    assert ran(ssh_calls(), "-i /keys/cli git@git.example.com git-receive-pack 'o/r.git'")


def test_no_account_sets_no_ssh_command():
    env = github_agent.shell_env(AgentAccountView("not_installed", absent=True))

    assert github_agent.SSH_PUSH_GUARD not in env.values()


@pytest.mark.parametrize("view", [LOCKED, READY], ids=["locked", "ready"])
def test_the_guard_is_in_the_names_safehouse_passes(view):
    from tools.shell_environment import shell_env_pass_names

    env = github_agent.shell_env(view)

    assert github_agent.SSH_PUSH_GUARD in env.values()
    assert set(env) <= set(shell_env_pass_names().split(","))
