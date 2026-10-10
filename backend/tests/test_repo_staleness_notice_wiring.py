"""End-to-end wiring proof for #936: the notice actually reaches model text.

``test_repo_staleness_notice.py`` covers the module's own logic in isolation.
This file proves the two call sites really deliver it: native
``core.turns_steps._append_tool_results`` (every result, not just the first)
and the harness bridge's real ``_with_notice_and_marker`` through
``subagents.harness_tool_publication._foreground_result`` (arguments plumbed
through the ``decorate_result`` seam).
"""

from __future__ import annotations

import functools
import os
import subprocess
import time
from pathlib import Path
from types import SimpleNamespace

import pytest

import core.repo_staleness_notice as staleness_mod
from infra.project_repo_policy import RepoPolicy


def _git(args: list[str], cwd: Path) -> None:
    env = {
        **os.environ,
        "GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@t.com",
        "GIT_COMMITTER_NAME": "t", "GIT_COMMITTER_EMAIL": "t@t.com",
    }
    subprocess.run(["git", *args], cwd=cwd, capture_output=True, text=True, check=True, env=env)


def _stale_repo(tmp_path: Path) -> Path:
    """Build a real repo pair and return the local clone, one commit behind."""
    remote = tmp_path / "remote.git"
    local = tmp_path / "local"
    other = tmp_path / "other"
    _git(["init", "-q", "--bare", str(remote)], cwd=tmp_path)
    _git(["clone", "-q", str(remote), str(local)], cwd=tmp_path)
    (local / "f.txt").write_text("one\n")
    _git(["add", "f.txt"], cwd=local)
    _git(["commit", "-q", "-m", "one"], cwd=local)
    _git(["branch", "-M", "main"], cwd=local)
    _git(["push", "-q", "-u", "origin", "main"], cwd=local)
    _git(["clone", "-q", str(remote), str(other)], cwd=tmp_path)
    (other / "f.txt").write_text("two\n")
    _git(["commit", "-q", "-am", "two"], cwd=other)
    _git(["push", "-q", "origin", "main"], cwd=other)
    return local


@pytest.fixture(autouse=True)
def _clean_state():
    staleness_mod._remote_cache.clear()
    staleness_mod._repeat_state.clear()
    yield
    staleness_mod._remote_cache.clear()
    staleness_mod._repeat_state.clear()


@pytest.fixture(autouse=True)
def _generous_git_timeouts(monkeypatch):
    """Setup probes real git; a loaded runner must not turn its production timeouts into a None."""
    monkeypatch.setattr(staleness_mod, "_LOCAL_GIT_TIMEOUT_SECONDS", 120)
    monkeypatch.setattr(staleness_mod, "_NETWORK_GIT_TIMEOUT_SECONDS", 120)


def _seed_stale(monkeypatch, local: Path) -> None:
    policy = RepoPolicy(
        project_id="p", repo_name="testrepo", path=local,
        pinned_branch=None, protect_base_checkout=True,
    )
    monkeypatch.setattr(staleness_mod, "_cached_policies", lambda: [policy])
    steps: list[str] = []
    real_run = staleness_mod._run_git

    def recording_run(repo_root, args, timeout):
        try:
            result = real_run(repo_root, args, timeout)
        except Exception as exc:
            steps.append(f"git {args[0]}: {type(exc).__name__}")
            raise
        steps.append(f"git {args[0]}: exit {result.returncode}")
        return result

    with monkeypatch.context() as scoped:
        scoped.setattr(staleness_mod, "_run_git", recording_run)
        probe = staleness_mod._probe(local)
    assert probe is not None and probe.behind is True, (
        f"probe of {local} returned {probe!r}; git steps run: {steps}"
    )
    staleness_mod._remote_cache[str(local)] = {
        "data": probe, "ts": time.monotonic(), "refreshing": False,
    }


@pytest.mark.asyncio
async def test_native_path_attaches_repo_notice_to_read_result(tmp_path, monkeypatch):
    from core.context_builder import ContextBuilder
    from core import turns_steps

    local = _stale_repo(tmp_path)
    _seed_stale(monkeypatch, local)
    monkeypatch.setattr(
        "core.tool_result_projection.render_tool_result_for_model",
        lambda _thread_id, canonical, budget_tokens: canonical.content,
    )
    monkeypatch.setattr(turns_steps, "_native_tool_media", lambda *_a, **_k: None)

    canonical = SimpleNamespace(role="tool", content="one\n", turn_id="turn-1")
    messages = await turns_steps._append_tool_results(
        SimpleNamespace(context=ContextBuilder(workspace=tmp_path), workspace=tmp_path),
        [],
        [(
            SimpleNamespace(id="call-1", name="read_file", arguments={"path": str(local / "f.txt")}),
            "one\n",
            canonical,
        )],
        "thread-1",
        512,
        "anthropic/selected",
    )

    assert len(messages) == 1
    content = messages[0]["content"]
    assert "one\n" in content
    assert "[CORSIA repo notice] Source repo 'testrepo'" in content
    assert "git pull --ff-only" in content


@pytest.mark.asyncio
async def test_native_path_resolves_relative_target_against_agent_workspace(
    tmp_path, monkeypatch,
):
    """A relative path on the native call site must resolve against the
    calling agent's own workspace, not the process-global ``WORKSPACE``.

    The prior absolute-path test never exercises this: an absolute target
    resolves the same way regardless of which workspace is passed. This
    mirrors the harness-bridge regression the reviewer flagged, on the
    native path where it was not yet exercised.
    """
    from infra.config import WORKSPACE
    from core.context_builder import ContextBuilder
    from core import turns_steps

    local = _stale_repo(tmp_path)
    assert local != WORKSPACE
    _seed_stale(monkeypatch, local)
    monkeypatch.setattr(
        "core.tool_result_projection.render_tool_result_for_model",
        lambda _thread_id, canonical, budget_tokens: canonical.content,
    )
    monkeypatch.setattr(turns_steps, "_native_tool_media", lambda *_a, **_k: None)

    canonical = SimpleNamespace(role="tool", content="one\n", turn_id="turn-1")
    messages = await turns_steps._append_tool_results(
        SimpleNamespace(context=ContextBuilder(workspace=tmp_path), workspace=local),
        [],
        [(
            # Relative: only resolves inside the stale repo if `workspace=`
            # is threaded through as `local`, not the global `WORKSPACE`.
            SimpleNamespace(id="call-1", name="read_file", arguments={"path": "f.txt"}),
            "one\n",
            canonical,
        )],
        "thread-1",
        512,
        "anthropic/selected",
    )

    content = messages[0]["content"]
    assert "[CORSIA repo notice] Source repo 'testrepo'" in content


@pytest.mark.asyncio
async def test_harness_bridge_path_attaches_repo_notice_to_read_result(tmp_path, monkeypatch):
    from subagents.harness_tool_bridge import _with_notice_and_marker
    from subagents.harness_tool_publication import _foreground_result
    from tools.recorded_call import CompletedToolCall

    local = _stale_repo(tmp_path)
    _seed_stale(monkeypatch, local)
    monkeypatch.setattr(
        "core.plan_notice.plan_notice",
        lambda **_kwargs: None,
    )

    async def persist_delivery(**_kwargs):
        return SimpleNamespace(
            invocation_id="inv", assistant_step_id="step",
            render_for_model=lambda **_kw: "one\n",
        )

    async def media_delivery(_delivery, _model):
        return None

    result = await _foreground_result(
        CompletedToolCall(
            name="read_file", result="one\n", invocation_id="inv",
            arguments={"path": str(local / "f.txt")},
        ),
        thread_id="thread-1", turn_identity=SimpleNamespace(turn_id="turn-1"),
        model=None, persist_delivery=persist_delivery,
        media_delivery=media_delivery, decorate_result=_with_notice_and_marker,
    )

    assert "one\n" in result
    assert "[CORSIA repo notice] Source repo 'testrepo'" in result
    assert "git pull --ff-only" in result


@pytest.mark.asyncio
async def test_harness_bridge_resolves_relative_path_against_its_own_workspace(
    tmp_path, monkeypatch,
):
    """A relative path must resolve against the tool's registry workspace.

    ``execute_tool`` binds ``workspace`` from the registry it was built with
    (``build_tool_registry(working_dir)``), which is commonly the registered
    source repo itself on the harness bridge -- not the process-global
    workspace. A regression here means the notice goes silent for exactly
    the case #936 targets: a relative ``read_file``/``grep``/``glob`` call,
    or ``grep``/``glob`` with no ``path`` at all, inside a stale checkout.
    """
    from subagents.harness_tool_bridge import _with_notice_and_marker
    from subagents.harness_tool_publication import _foreground_result
    from tools.recorded_call import CompletedToolCall

    local = _stale_repo(tmp_path)
    _seed_stale(monkeypatch, local)
    monkeypatch.setattr("core.plan_notice.plan_notice", lambda **_kwargs: None)

    async def persist_delivery(**_kwargs):
        return SimpleNamespace(
            invocation_id="inv", assistant_step_id="step",
            render_for_model=lambda **_kw: "one\n",
        )

    async def media_delivery(_delivery, _model):
        return None

    decorate_result = functools.partial(_with_notice_and_marker, workspace=local)
    result = await _foreground_result(
        CompletedToolCall(
            name="read_file", result="one\n", invocation_id="inv",
            arguments={"path": "f.txt"},  # relative: resolves against `local`, not WORKSPACE
        ),
        thread_id="thread-1", turn_identity=SimpleNamespace(turn_id="turn-1"),
        model=None, persist_delivery=persist_delivery,
        media_delivery=media_delivery, decorate_result=decorate_result,
    )

    assert "[CORSIA repo notice] Source repo 'testrepo'" in result


def test_registered_filesystem_tools_carry_their_own_workspace(tmp_path):
    """``execute_tool`` resolves workspace from the dispatched tool instance.

    A registry-level ``workspace`` field would be a duplicate of the fact
    each filesystem tool already carries on itself (``Tool.workspace``,
    backed by ``self._workspace``), with nothing keeping the two in sync.
    So every construction path must set it correctly on the *tool*, not on
    the registry: this covers ``CORSIAAgent``'s own registration and the
    harness bridge's ``build_tool_registry``.
    """
    from core.agent import CORSIAAgent, _register_tools
    from infra.settings import get_tool_config
    from subagents.harness_tool_bridge import build_tool_registry

    agent = CORSIAAgent(workspace=tmp_path)
    _register_tools(agent, get_tool_config(), tmp_path)
    assert agent.tools.get("read_file").workspace == tmp_path

    bridge_registry = build_tool_registry(str(tmp_path))
    assert bridge_registry.get("read_file").workspace == tmp_path
