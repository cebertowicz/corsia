"""Tests for the stale-repo warning (#936): real temporary git repositories.

Structural cases (behind, ahead-only, diverged, current, detached HEAD, no
remote, unreachable remote) run against real git repos so the git semantics —
not a mock of them — are what's under test. Only the network timeout case
patches ``subprocess.run``, mirroring ``test_update_status.py``'s precedent for
the one case a real repo cannot reproduce deterministically and quickly.
"""

from __future__ import annotations

import os
import subprocess
import time
from pathlib import Path

import pytest

import core.repo_staleness_notice as mod
from infra.project_repo_policy import RepoPolicy


def _git(args: list[str], cwd: Path) -> subprocess.CompletedProcess[str]:
    env = {
        **os.environ,
        "GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@t.com",
        "GIT_COMMITTER_NAME": "t", "GIT_COMMITTER_EMAIL": "t@t.com",
    }
    return subprocess.run(
        ["git", *args], cwd=cwd, capture_output=True, text=True, check=True, env=env,
    )


def _repo_pair(tmp_path: Path, branch: str = "main") -> tuple[Path, Path]:
    """Return (remote bare repo, local clone) with one commit pushed to ``branch``."""
    remote = tmp_path / "remote.git"
    local = tmp_path / "local"
    _git(["init", "-q", "--bare", str(remote)], cwd=tmp_path)
    _git(["clone", "-q", str(remote), str(local)], cwd=tmp_path)
    (local / "f.txt").write_text("one\n")
    _git(["add", "f.txt"], cwd=local)
    _git(["commit", "-q", "-m", "one"], cwd=local)
    _git(["branch", "-M", branch], cwd=local)
    _git(["push", "-q", "-u", "origin", branch], cwd=local)
    return remote, local


def _push_extra_commit(remote: Path, tmp_path: Path, branch: str, name: str) -> None:
    """Push one more commit to ``remote`` from a throwaway clone another agent owns."""
    other = tmp_path / name
    # Name the branch: the bare remote's HEAD follows the host's
    # init.defaultBranch, which need not be the branch pushed.
    _git(["clone", "-q", "-b", branch, str(remote), str(other)], cwd=tmp_path)
    (other / "f.txt").write_text(f"{name}\n")
    _git(["commit", "-q", "-am", name], cwd=other)
    _git(["push", "-q", "origin", branch], cwd=other)


def _policy(repo_root: Path, name: str = "testrepo") -> RepoPolicy:
    return RepoPolicy(
        project_id="p", repo_name=name, path=repo_root,
        pinned_branch=None, protect_base_checkout=True,
    )


@pytest.fixture(autouse=True)
def _generous_git_timeouts(monkeypatch):
    """The structural cases probe real git; a loaded runner must not turn its production
    timeouts into the None that the unknown-state cases assert."""
    monkeypatch.setattr(mod, "_LOCAL_GIT_TIMEOUT_SECONDS", 120)
    monkeypatch.setattr(mod, "_NETWORK_GIT_TIMEOUT_SECONDS", 120)


def _use_policy(monkeypatch, policy: RepoPolicy) -> None:
    monkeypatch.setattr(mod, "_cached_policies", lambda: [policy])


def _seed_probe(monkeypatch, repo_root: Path, probe: "mod._RepoProbe | None") -> None:
    """Seed the cache directly, bypassing the background thread, for a fast unit test."""
    mod._remote_cache[str(repo_root)] = {
        "data": probe, "ts": time.monotonic(), "refreshing": False,
    }


@pytest.fixture(autouse=True)
def _clean_module_state():
    mod._remote_cache.clear()
    mod._repeat_state.clear()
    mod._policy_cache["data"] = None
    mod._policy_cache["ts"] = 0.0
    yield
    mod._remote_cache.clear()
    mod._repeat_state.clear()


def _wait_for_probe(repo_root: Path, timeout: float = 5.0) -> "mod._RepoProbe | None":
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        entry = mod._remote_cache.get(str(repo_root))
        if entry is not None and not entry["refreshing"]:
            return entry["data"]
        time.sleep(0.02)
    raise AssertionError("background probe did not complete in time")


# --- structural cases against real repos -----------------------------------


def test_behind_reports_no_count_and_notice_fires(tmp_path, monkeypatch):
    remote, local = _repo_pair(tmp_path)
    _push_extra_commit(remote, tmp_path, "main", "other")
    _use_policy(monkeypatch, _policy(local))

    probe = mod._probe(local)

    assert probe is not None
    assert probe.behind is True
    assert probe.commits_behind is None  # remote commit was never fetched
    assert probe.branch == "main"

    _seed_probe(monkeypatch, local, probe)
    notice = mod.repo_staleness_notice(
        thread_id="t", tool_name="read_file", arguments={"path": str(local / "f.txt")},
    )
    assert notice is not None
    assert "[CORSIA repo notice] Source repo 'testrepo' (branch main)" in notice
    assert "is behind origin/main: the remote has commits this checkout does not" in notice
    assert "git pull --ff-only" in notice
    assert str(local) in notice
    assert "work in a worktree created from origin/main instead" in notice


def test_ahead_only_is_not_behind(tmp_path, monkeypatch):
    _remote, local = _repo_pair(tmp_path)
    (local / "f.txt").write_text("unpushed\n")
    _git(["commit", "-q", "-am", "unpushed"], cwd=local)
    _use_policy(monkeypatch, _policy(local))

    probe = mod._probe(local)

    assert probe is not None
    assert probe.behind is False


def test_diverged_is_behind(tmp_path, monkeypatch):
    """A diverged checkout is classified as behind, same as the plain case.

    It cannot be told apart from plain-behind without a fetch (module
    docstring), so the notice must name a remedy that holds for both: the
    fast-forward for the common case, with the refusal case covered by the
    same fallback sentence -- never a claim that pull will just work.
    """
    remote, local = _repo_pair(tmp_path)
    (local / "f.txt").write_text("local-only\n")
    _git(["commit", "-q", "-am", "local-only"], cwd=local)
    _push_extra_commit(remote, tmp_path, "main", "other")
    _use_policy(monkeypatch, _policy(local))

    probe = mod._probe(local)

    assert probe is not None
    assert probe.behind is True

    _seed_probe(monkeypatch, local, probe)
    notice = mod.repo_staleness_notice(
        thread_id="t", tool_name="read_file", arguments={"path": str(local / "f.txt")},
    )
    assert notice is not None
    assert "git pull --ff-only" in notice
    assert "if it refuses because this checkout has its own commits" in notice
    assert "work in a worktree created from origin/main instead" in notice


def test_up_to_date_is_silent(tmp_path, monkeypatch):
    _remote, local = _repo_pair(tmp_path)
    _use_policy(monkeypatch, _policy(local))

    probe = mod._probe(local)

    assert probe is not None
    assert probe.behind is False

    _seed_probe(monkeypatch, local, probe)
    notice = mod.repo_staleness_notice(
        thread_id="t", tool_name="read_file", arguments={"path": str(local / "f.txt")},
    )
    assert notice is None


def test_detached_head_is_unknown(tmp_path, monkeypatch):
    _remote, local = _repo_pair(tmp_path)
    _git(["checkout", "-q", "--detach", "HEAD"], cwd=local)
    _use_policy(monkeypatch, _policy(local))

    assert mod._probe(local) is None


def test_no_remote_is_unknown(tmp_path, monkeypatch):
    _remote, local = _repo_pair(tmp_path)
    _git(["remote", "remove", "origin"], cwd=local)
    _use_policy(monkeypatch, _policy(local))

    assert mod._probe(local) is None


def test_no_such_remote_branch_is_unknown(tmp_path, monkeypatch):
    _remote, local = _repo_pair(tmp_path)
    _git(["checkout", "-q", "-b", "feature"], cwd=local)
    _use_policy(monkeypatch, _policy(local))

    assert mod._probe(local) is None


def test_unreachable_remote_is_unknown(tmp_path, monkeypatch):
    _remote, local = _repo_pair(tmp_path)
    _git(["remote", "set-url", "origin", str(tmp_path / "does-not-exist.git")], cwd=local)
    _use_policy(monkeypatch, _policy(local))

    assert mod._probe(local) is None


def test_timeout_is_unknown(tmp_path, monkeypatch):
    _remote, local = _repo_pair(tmp_path)
    _use_policy(monkeypatch, _policy(local))
    real_run = mod._run_git

    def hanging_run(repo_root, args, timeout):
        if args[:1] == ["ls-remote"]:
            raise subprocess.TimeoutExpired(cmd="git ls-remote", timeout=timeout)
        return real_run(repo_root, args, timeout)

    monkeypatch.setattr(mod, "_run_git", hanging_run)

    assert mod._probe(local) is None


# --- no network call / no .git write on the call path -----------------------


def test_probe_makes_no_git_writes(tmp_path, monkeypatch):
    remote, local = _repo_pair(tmp_path)
    _push_extra_commit(remote, tmp_path, "main", "other")
    fetch_head = local / ".git" / "FETCH_HEAD"
    objects_before = sorted((local / ".git" / "objects").rglob("*"))

    mod._probe(local)

    assert not fetch_head.exists()
    assert sorted((local / ".git" / "objects").rglob("*")) == objects_before
    status = _git(["status", "--porcelain"], cwd=local)
    assert status.stdout == ""


def test_cold_cache_is_silent_and_call_path_has_no_network_call(tmp_path, monkeypatch):
    remote, local = _repo_pair(tmp_path)
    _push_extra_commit(remote, tmp_path, "main", "other")
    _use_policy(monkeypatch, _policy(local))

    def refuse_ls_remote(repo_root, args, timeout):
        raise AssertionError("no git subprocess must run synchronously on the call path")

    monkeypatch.setattr(mod, "_run_git", refuse_ls_remote)

    started = time.monotonic()
    notice = mod.repo_staleness_notice(
        thread_id="t", tool_name="read_file", arguments={"path": str(local / "f.txt")},
    )
    elapsed = time.monotonic() - started

    assert notice is None  # cold cache: silent
    assert elapsed < 0.5  # answered from cache lookup alone, no subprocess


def test_background_refresh_populates_cache_then_notice_fires(tmp_path, monkeypatch):
    remote, local = _repo_pair(tmp_path)
    _push_extra_commit(remote, tmp_path, "main", "other")
    _use_policy(monkeypatch, _policy(local))

    first = mod.repo_staleness_notice(
        thread_id="t", tool_name="read_file", arguments={"path": str(local / "f.txt")},
    )
    assert first is None  # cold

    probe = _wait_for_probe(local)
    assert probe is not None and probe.behind is True

    second = mod.repo_staleness_notice(
        thread_id="t", tool_name="read_file", arguments={"path": str(local / "f.txt")},
    )
    assert second is not None


# --- an unexpected probe failure must never be indistinguishable from healthy --


def test_probe_failure_is_logged_and_emitted_not_swallowed_silently(
    tmp_path, monkeypatch, caplog,
):
    """A bug or environment fault in ``_probe`` must leave a trace.

    Every expected "unknown" outcome already returns None explicitly inside
    ``_probe``/``_probe_ancestry``; only a real fault reaches this catch-all,
    so silently discarding it would make a fully dead detector look exactly
    like a healthy, current repo.
    """
    remote, local = _repo_pair(tmp_path)
    _push_extra_commit(remote, tmp_path, "main", "other")
    _use_policy(monkeypatch, _policy(local))
    events: list[tuple[str, str, dict]] = []
    monkeypatch.setattr(
        mod, "emit_product_event",
        lambda category, action, **kw: events.append((category, action, kw)),
    )

    def boom(_repo_root):
        raise RuntimeError("simulated probe bug")

    monkeypatch.setattr(mod, "_probe", boom)

    import logging
    with caplog.at_level(logging.WARNING, logger="core.repo_staleness_notice"):
        mod._refresh_probe(local, str(local))

    assert mod._remote_cache[str(local)]["data"] is None
    assert any("probe failed" in record.message for record in caplog.records)
    assert events == [
        ("repo_staleness_notice", "probe_failed", {
            "properties": {"repo_root": str(local), "error": "simulated probe bug"},
        }),
    ]


def test_unreachable_remote_is_logged_and_emitted_not_indistinguishable_from_healthy(
    tmp_path, monkeypatch, caplog,
):
    """A failing ``git ls-remote`` must leave a trace, not read as "not behind".

    Unreachable remote, missing auth, and no-such-branch all exit non-zero
    here and previously returned ``None`` with no signal at all — identical
    to the healthy "nothing to warn about" answer, so a backend without SSH
    agent access or offline could leave the whole feature permanently dead
    with zero operator visibility.
    """
    _remote, local = _repo_pair(tmp_path)
    real_run = mod._run_git

    def failing_ls_remote(repo_root, args, timeout):
        if args[:1] == ["ls-remote"]:
            return subprocess.CompletedProcess(
                args=["git", *args], returncode=128, stdout="", stderr="fatal: no such remote\n",
            )
        return real_run(repo_root, args, timeout)

    monkeypatch.setattr(mod, "_run_git", failing_ls_remote)
    events: list[tuple[str, str, dict]] = []
    monkeypatch.setattr(
        mod, "emit_product_event",
        lambda category, action, **kw: events.append((category, action, kw)),
    )

    import logging
    with caplog.at_level(logging.WARNING, logger="core.repo_staleness_notice"):
        result = mod._probe(local)

    assert result is None
    assert any("remote probe failed" in record.message for record in caplog.records)
    assert events == [
        ("repo_staleness_notice", "probe_unreachable", {
            "properties": {"repo_root": str(local), "exit_code": 128},
        }),
    ]


def test_policy_query_failure_returns_empty_not_raised(tmp_path, monkeypatch, caplog):
    """``_cached_policies`` must never fail the tool result it rides on.

    ``list_registered_repo_policies`` raises ``RepoPolicyQueryError`` on a
    malformed project identity or a failed DB query; every other non-test
    caller in the tree catches it, and this best-effort advisory notice must
    too, or a tool-result delivery breaks on a query fault unrelated to the
    read the model actually asked for.
    """
    from infra.project_repo_policy import RepoPolicyQueryError

    def boom(**_kwargs):
        raise RepoPolicyQueryError("simulated query fault")

    monkeypatch.setattr(mod, "list_registered_repo_policies", boom)
    events: list[tuple[str, str, dict]] = []
    monkeypatch.setattr(
        mod, "emit_product_event",
        lambda category, action, **kw: events.append((category, action, kw)),
    )

    import logging
    with caplog.at_level(logging.WARNING, logger="core.repo_staleness_notice"):
        result = mod._cached_policies()

    assert result == []
    assert any("policy query failed" in record.message for record in caplog.records)
    assert events == [("repo_staleness_notice", "policy_query_failed", {})]

    notice = mod.repo_staleness_notice(
        thread_id="t", tool_name="read_file", arguments={"path": str(tmp_path / "f.txt")},
    )
    assert notice is None


def test_notice_emission_reports_a_product_event(tmp_path, monkeypatch):
    remote, local = _repo_pair(tmp_path)
    _push_extra_commit(remote, tmp_path, "main", "other")
    _use_policy(monkeypatch, _policy(local))
    events: list[tuple[str, str, dict]] = []
    monkeypatch.setattr(
        mod, "emit_product_event",
        lambda category, action, **kw: events.append((category, action, kw)),
    )

    _seed_probe(monkeypatch, local, mod._probe(local))
    notice = mod.repo_staleness_notice(
        thread_id="t", tool_name="read_file", arguments={"path": str(local / "f.txt")},
    )

    assert notice is not None
    assert len(events) == 1
    category, action, kwargs = events[0]
    assert (category, action) == ("repo_staleness_notice", "notice_emitted")
    assert kwargs["thread_id"] == "t"
    assert kwargs["properties"]["repo_name"] == "testrepo"


# --- scope: worktrees and non-repo paths get nothing ------------------------


def test_worktree_path_gets_no_notice(tmp_path, monkeypatch):
    remote, local = _repo_pair(tmp_path)
    _push_extra_commit(remote, tmp_path, "main", "other")
    worktree_dir = local / ".worktrees" / "wt1"
    worktree_dir.parent.mkdir(parents=True, exist_ok=True)
    _git(["worktree", "add", "-q", "-b", "wt1-branch", str(worktree_dir)], cwd=local)
    _use_policy(monkeypatch, _policy(local))
    _seed_probe(monkeypatch, local, mod._probe(local))

    notice = mod.repo_staleness_notice(
        thread_id="t", tool_name="read_file",
        arguments={"path": str(worktree_dir / "f.txt")},
    )
    assert notice is None


def test_path_outside_any_registered_repo_gets_no_notice(tmp_path, monkeypatch):
    monkeypatch.setattr(mod, "_cached_policies", lambda: [])
    notice = mod.repo_staleness_notice(
        thread_id="t", tool_name="read_file", arguments={"path": str(tmp_path / "x.txt")},
    )
    assert notice is None


# --- tool/argument resolution ------------------------------------------------


def test_bash_is_out_of_scope_and_never_looks_up_policy(monkeypatch):
    def boom():
        raise AssertionError("bash must never trigger a policy lookup")

    monkeypatch.setattr(mod, "_cached_policies", boom)
    assert mod.repo_staleness_notice(
        thread_id="t", tool_name="bash", arguments={"command": "ls"},
    ) is None


def test_non_dict_arguments_is_silent():
    assert mod.repo_staleness_notice(
        thread_id="t", tool_name="read_file", arguments="not-a-dict",
    ) is None


def test_grep_defaults_target_to_workspace_when_path_omitted(tmp_path, monkeypatch):
    monkeypatch.setattr(mod, "WORKSPACE", tmp_path)
    src_dir = tmp_path / "src"
    src_dir.mkdir()
    remote, local = _repo_pair(src_dir)
    _push_extra_commit(remote, src_dir, "main", "other")
    _use_policy(monkeypatch, _policy(local))
    _seed_probe(monkeypatch, local, mod._probe(local))

    notice = mod.repo_staleness_notice(
        thread_id="t", tool_name="grep", arguments={"pattern": "x"},
    )
    assert notice is None  # "." resolves to WORKSPACE, outside the registered repo


# --- repeat rule: once per 10 minutes per thread, again if the head moves ---


def test_repeat_rule_suppresses_then_reemits_after_window(monkeypatch):
    monkeypatch.setattr(mod, "_REPEAT_WINDOW_SECONDS", 0.05)
    assert mod._should_emit("t", "repo", "sha1") is True
    assert mod._should_emit("t", "repo", "sha1") is False
    time.sleep(0.1)
    assert mod._should_emit("t", "repo", "sha1") is True


def test_repeat_rule_reemits_immediately_when_remote_head_changes(monkeypatch):
    monkeypatch.setattr(mod, "_REPEAT_WINDOW_SECONDS", 600)
    assert mod._should_emit("t", "repo", "sha1") is True
    assert mod._should_emit("t", "repo", "sha1") is False
    assert mod._should_emit("t", "repo", "sha2") is True


def test_repeat_rule_is_scoped_per_thread(monkeypatch):
    monkeypatch.setattr(mod, "_REPEAT_WINDOW_SECONDS", 600)
    assert mod._should_emit("thread-a", "repo", "sha1") is True
    assert mod._should_emit("thread-b", "repo", "sha1") is True


def test_repeat_state_prunes_expired_entries_on_write(monkeypatch):
    """``_repeat_state`` must not grow without bound across many threads/repos."""
    monkeypatch.setattr(mod, "_REPEAT_WINDOW_SECONDS", 0.05)
    for i in range(5):
        mod._should_emit(f"thread-{i}", "repo", "sha1")
    assert len(mod._repeat_state) == 5

    time.sleep(0.1)
    mod._should_emit("thread-new", "repo", "sha1")  # write triggers the prune

    assert len(mod._repeat_state) == 1
    assert ("thread-new", "repo") in mod._repeat_state


# --- wording: the commit count is claimed only when verifiable locally ------


def test_notice_includes_commit_count_when_remote_object_is_locally_present(
    tmp_path, monkeypatch,
):
    remote, local = _repo_pair(tmp_path)
    _push_extra_commit(remote, tmp_path, "main", "other")
    # Simulate the rare case the remote tip is already present locally (e.g. a
    # prior fetch under another ref) by fetching it directly for this test only.
    _git(["fetch", "-q", "origin", "main"], cwd=local)
    _use_policy(monkeypatch, _policy(local))

    probe = mod._probe(local)
    assert probe is not None
    assert probe.behind is True
    assert probe.commits_behind == 1

    _seed_probe(monkeypatch, local, probe)
    notice = mod.repo_staleness_notice(
        thread_id="t", tool_name="read_file", arguments={"path": str(local / "f.txt")},
    )
    assert "1 commit behind origin/main" in notice


# --- path resolution shares the one rule tools own, not a private copy -----


def test_resolve_target_matches_the_filesystem_gates_own_join_and_resolve(tmp_path):
    """This module must never re-derive the join/resolve rule tools already own.

    ``_resolve_target`` delegates to ``tools.filesystem_gate.join_and_resolve``
    so a future change to how tools compute their own target path cannot
    silently desync this module's notion of "what did this read touch" from
    theirs (the concern EAS2 raised in review).
    """
    from tools.filesystem_gate import join_and_resolve

    workspace = tmp_path / "workspace"
    workspace.mkdir()
    assert mod._resolve_target("relative/f.txt", workspace) == join_and_resolve(
        "relative/f.txt", workspace,
    )
    absolute = str(tmp_path / "elsewhere" / "f.txt")
    assert mod._resolve_target(absolute, workspace) == join_and_resolve(absolute, workspace)
