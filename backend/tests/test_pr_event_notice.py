"""The one-time PR notice at both tool-result seams (design §7 tests 1, 3, 4).

Ownership is real registry state in a disposable database; the notice is read
back from the model text each seam produces.
"""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

import pytest

from events.connectors import github_pr_notice as notices
from events.identity import GITHUB_INTEGRATION, PR_TYPE, pr_external_id
from infra.settings import get_setting
from tests.event_routing_helpers import mk_session, register_pr

REPO = "o/r"
CREATE = {"command": "gh pr create --draft --base dev --title t --body b"}


@pytest.fixture(autouse=True)
def _fresh_caches():
    notices._owned_cache.clear()
    notices._last_poll_notice.clear()
    yield
    notices._owned_cache.clear()
    notices._last_poll_notice.clear()


@pytest.fixture()
def clock(monkeypatch):
    now = {"t": 1_000.0}
    monkeypatch.setattr(notices.time, "monotonic", lambda: now["t"])
    return now


def _webhook(on: bool) -> None:
    from infra import key_store

    if on:
        key_store.set_value("github-webhook", "hook-value-for-tests")
    else:
        key_store.clear_value("github-webhook")


def _notice(thread_id: str, tool: str = "bash", arguments: dict | None = None) -> str | None:
    return notices.pr_event_notice(
        thread_id=thread_id, tool_name=tool, arguments=arguments or {"command": "ls"},
    )


async def _native_result(thread_id: str, monkeypatch, arguments: dict) -> str:
    from core import turns_steps
    from core.context_builder import ContextBuilder

    monkeypatch.setattr(
        "core.tool_result_projection.render_tool_result_for_model",
        lambda _thread_id, canonical, budget_tokens: canonical.content,
    )
    monkeypatch.setattr(turns_steps, "_native_tool_media", lambda *_a, **_k: None)
    monkeypatch.setattr("core.plan_notice.plan_notice", lambda **_kwargs: None)
    canonical = SimpleNamespace(role="tool", content="created\n", turn_id="turn-1")
    messages = await turns_steps._append_tool_results(
        SimpleNamespace(context=ContextBuilder(workspace=Path("/tmp")), workspace=Path("/tmp")),
        [],
        [(SimpleNamespace(id="call-1", name="bash", arguments=arguments), "created\n", canonical)],
        thread_id,
        512,
        "anthropic/selected",
    )
    return messages[0]["content"]


async def _bridge_result(thread_id: str, monkeypatch, arguments: dict) -> str:
    from subagents.harness_tool_bridge import _with_notice_and_marker
    from subagents.harness_tool_publication import _foreground_result
    from tools.recorded_call import CompletedToolCall

    monkeypatch.setattr("core.plan_notice.plan_notice", lambda **_kwargs: None)

    async def persist_delivery(**_kwargs):
        return SimpleNamespace(
            invocation_id="inv", assistant_step_id="step",
            render_for_model=lambda **_kw: "created\n",
        )

    async def media_delivery(_delivery, _model):
        return None

    return await _foreground_result(
        CompletedToolCall(name="bash", result="created\n", invocation_id="inv", arguments=arguments),
        thread_id=thread_id, turn_identity=SimpleNamespace(turn_id="turn-1"),
        model=None, persist_delivery=persist_delivery,
        media_delivery=media_delivery, decorate_result=_with_notice_and_marker,
    )


# -- Test 1: the create result carries it once, on both seams; a rehome re-notices --


@pytest.mark.asyncio
@pytest.mark.parametrize("seam", [_native_result, _bridge_result], ids=["native", "bridge"])
async def test_notice_rides_the_create_result_once_per_owner(isolated_db, monkeypatch, seam):
    _webhook(True)
    owner = mk_session("notice-owner")
    # An earlier call in the same minute cached "owns nothing"; the create must see past it.
    assert "[CORSIA PR notice]" not in await seam(owner, monkeypatch, {"command": "ls"})
    register_pr(REPO, 12, owner)

    first = await seam(owner, monkeypatch, CREATE)
    second = await seam(owner, monkeypatch, {"command": "ls"})

    assert first.startswith("created\n")
    assert "[CORSIA PR notice] You own PR o/r#12." in first
    assert "Do not poll `gh pr checks` or `gh pr view`." in first
    assert "[CORSIA PR notice]" not in second
    assert get_setting(f"pr_notice:{owner}:o/r#12") is not None


def test_a_rehomed_pr_is_noticed_again_for_its_new_owner(isolated_db, clock):
    from registry import models

    old, new = mk_session("rehome-old"), mk_session("rehome-new")
    register_pr(REPO, 13, old)
    assert "o/r#13" in (_notice(old, "bash", CREATE) or "")
    models.rehome_artifact(
        integration=GITHUB_INTEGRATION, type=PR_TYPE, external_id=pr_external_id(REPO, 13),
        new_owner_thread_id=new, actor_thread_id=old, authority="current-owner-handoff",
        reason="handoff", source_path="test",
    )

    assert "You own PR o/r#13" in (_notice(new) or "")
    assert _notice(new) is None


def test_a_subscriber_is_not_told_it_owns_the_pr(isolated_db):
    from registry import models

    owner, watcher = mk_session("sub-owner"), mk_session("sub-watcher")
    register_pr(REPO, 17, owner)
    models.register_artifact(
        integration=GITHUB_INTEGRATION, type=PR_TYPE, external_id=pr_external_id(REPO, 17),
        parent_thread_id=watcher, title="PR #17",
    )
    state = get_db_row_state(watcher)
    assert state == "subscriber-recent"
    assert _notice(watcher) is None


def test_the_owned_pr_read_is_guarded_for_sixty_seconds(isolated_db, clock):
    owner = mk_session("guard-owner")
    assert _notice(owner) is None
    register_pr(REPO, 14, owner)

    assert _notice(owner) is None, "an ordinary call inside 60 s reuses the cached read"
    clock["t"] += notices.QUERY_GUARD_S + 1
    assert "You own PR o/r#14" in (_notice(owner) or "")


def get_db_row_state(thread_id: str) -> str:
    from infra.database import get_db

    return get_db().execute(
        "SELECT relationship_state FROM artifact_registrations WHERE parent_thread_id=?",
        (thread_id,),
    ).fetchone()[0]


# -- Test 3: literal poll commands re-attach it, at most every 30 min --


def test_poll_command_reattaches_the_notice_for_an_owned_open_pr(isolated_db, clock):
    owner = mk_session("poll-owner")
    register_pr(REPO, 12, owner)
    register_pr(REPO, 15, owner)
    assert _notice(owner) is not None and _notice(owner) is not None  # both noticed once
    assert _notice(owner) is None

    polled = _notice(owner, "bash", {"command": "gh pr checks 12"})
    assert polled is not None and "PR o/r#12." in polled, "names the PR the command polls"
    assert _notice(owner, "bash", {"command": "gh pr view 12 --json state"}) is None
    clock["t"] += notices.POLL_NOTICE_INTERVAL_S + 1
    assert "PR o/r#15." in (_notice(owner, "bash", {"command": "gh run watch 999"}) or "")


def test_poll_command_is_silent_for_unowned_and_ended_prs(isolated_db, clock):
    from registry import pr_lifecycle

    stranger = mk_session("poll-stranger")
    assert _notice(stranger, "bash", {"command": "gh pr checks 12"}) is None

    owner = mk_session("poll-ended")
    register_pr(REPO, 16, owner)
    assert _notice(owner) is not None
    pr_lifecycle.record_pull_request_ended(
        GITHUB_INTEGRATION, PR_TYPE, pr_external_id(REPO, 16), pr_state="merged",
    )
    clock["t"] += notices.QUERY_GUARD_S + 1
    assert _notice(owner, "bash", {"command": "gh pr checks 16"}) is None


@pytest.mark.parametrize("command,matches", [
    ("gh pr checks 12", True),
    ("  gh run view 22 --log-failed", True),
    ("gh pr view", True),
    ("echo gh pr checks 12", False),
    ("gh pr create --draft", False),
    ("gh prs checks", False),
])
def test_poll_detection_matches_only_literal_polling_commands(command, matches):
    assert (notices._POLL_COMMAND.match(notices._bash_command("bash", {"command": command}) or "")
            is not None) is matches
    assert notices._bash_command("read_file", {"command": command}) is None


# -- Test 4 (notice half) and finding 5: wording follows the webhook endpoint's secret --


def test_wording_follows_the_webhook_endpoints_own_secret(isolated_db, monkeypatch):
    from api import webhook_receivers

    pr = notices.OwnedPr("o/r#12", REPO, 12, False)
    _webhook(False)
    assert webhook_receivers.receiver_secret("github") is None
    poller_only = notices._notice(pr)
    assert "This instance does not receive CI or reviewer results." in poller_only
    assert "run `gh pr checks 12` once. Do not poll in a loop." in poller_only

    _webhook(True)
    assert "CORSIA wakes you when its CI finishes" in notices._notice(pr)

    monkeypatch.setattr(webhook_receivers, "receiver_secret", lambda connector: None)
    assert notices.webhook_configured() is False, "reads through the endpoint's helper"
