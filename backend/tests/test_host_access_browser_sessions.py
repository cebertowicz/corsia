"""Scoped owner control-flow tests. Real ownership proof is in owned_processes.

The previous autouse env=True/999999-second fixture is intentionally removed.
Only scheduling/formatting seams below are mocked; none claims daemon identity.
"""
import asyncio
from dataclasses import replace

import pytest
from host_access import browser_cli, browser_sessions as sessions
from tests.browser_process_fixture import synthetic_browser  # noqa: F401


async def test_opaque_sessions_and_canonical_native_argv(synthetic_browser):
    fake = synthetic_browser
    await fake.owner.run('raw-a', ['snapshot'])
    await fake.owner.run('raw-a', ['snapshot'])
    await fake.owner.run('raw-b', ['snapshot'])
    assert fake.calls[0][2] == fake.calls[1][2] != fake.calls[2][2]
    for call in fake.calls:
        assert call[0] == fake.owner._native.path
        assert call[1] == '--session'
        assert 'raw-a' not in call and 'raw-b' not in call
        assert '--namespace' not in call and 'close' not in call
    assert fake.owner.live_count() == 2
    await fake.owner.shutdown()


async def test_visible_browser_mode_is_pinned_when_the_session_starts(synthetic_browser):
    fake = synthetic_browser
    await fake.owner.run("visible", ["open", "about:blank"], show_window=True)
    await fake.owner.run("headless", ["open", "about:blank"], show_window=False)
    visible, headless = fake.calls
    assert "--headed" in visible
    assert "--headed" not in headless
    await fake.owner.shutdown()


@pytest.mark.parametrize("chrome", ["/Applications/Google Chrome.app/Contents/MacOS/Google Chrome", None])
async def test_chosen_chrome_is_a_launch_option_resolved_once(synthetic_browser, monkeypatch, chrome):
    fake = synthetic_browser
    resolved = []
    monkeypatch.setattr(browser_cli, "chrome_executable",
                        lambda: resolved.append(chrome) or (chrome, "system" if chrome else "default"))
    await fake.owner.run("visible", ["open", "about:blank"], show_window=True)
    await fake.owner.run("visible", ["snapshot"])
    await fake.owner.run("headless", ["snapshot"])
    assert resolved == [chrome]
    for call in fake.calls:
        launch = call[:call.index("--json")]
        assert launch[1] == "--session"
        assert (launch[3:5] == ("--executable-path", chrome)) if chrome else ("--executable-path" not in call)
        # Only the binary changes: agent-browser keeps its temporary profile, never the person's.
        assert not {"--profile", "--user-data-dir", "--args"} & set(call)
    assert "--headed" in fake.calls[0]
    await fake.owner.shutdown()


@pytest.mark.parametrize(
    ("initial", "requested", "current"),
    [(False, True, "headless"), (True, False, "visible")],
)
async def test_mode_change_requires_closing_the_existing_session(
    synthetic_browser, initial, requested, current,
):
    fake = synthetic_browser
    await fake.owner.run("thread", ["snapshot"], show_window=initial)
    requested_value = "true" if requested else "false"
    with pytest.raises(
        sessions.BrowserSessionError,
        match=f"running {current}.*browser_close.*show_window={requested_value}",
    ):
        await fake.owner.run("thread", ["open", "about:blank"], show_window=requested)
    assert len(fake.calls) == 1
    await fake.owner.shutdown()


async def test_active_count_includes_running_and_queued(synthetic_browser, monkeypatch):
    fake = synthetic_browser
    await fake.owner.run('thread', ['snapshot'])
    entered, release = asyncio.Event(), asyncio.Event()
    original = fake.invoke

    async def paused(argv, *, timeout):
        entered.set()
        await release.wait()
        return await original(argv, timeout=timeout)

    monkeypatch.setattr(fake.owner, '_invoke', paused)
    first = asyncio.create_task(fake.owner.run('thread', ['snapshot']))
    await entered.wait()
    second = asyncio.create_task(fake.owner.run('thread', ['snapshot']))
    await asyncio.sleep(0.05)
    assert fake.owner.active_command_count() == 2
    second.cancel()
    with pytest.raises(asyncio.CancelledError):
        await second
    assert fake.owner.active_command_count() == 1
    release.set()
    await first
    assert fake.owner.active_command_count() == 0
    await fake.owner.shutdown()


@pytest.mark.parametrize('followup', ['new', 'thread'])
async def test_lease_fences_new_and_unlabelled_bound_work(synthetic_browser, monkeypatch, followup):
    fake = synthetic_browser
    await fake.owner.run('thread', ['snapshot'], lease_id='lease')
    entered, release = asyncio.Event(), asyncio.Event()
    original = fake.owner._close_runtime

    async def paused(runtime):
        entered.set()
        await release.wait()
        await original(runtime)

    monkeypatch.setattr(fake.owner, '_close_runtime', paused)
    task = asyncio.create_task(fake.owner.terminate_lease('lease'))
    await entered.wait()
    try:
        with pytest.raises(sessions.BrowserSessionError, match='admission is closed|session unavailable'):
            await fake.owner.run(followup, ['snapshot'],
                                 lease_id='lease' if followup == 'new' else None)
        assert len(fake.calls) == 1
    finally:
        release.set()
        await task


async def test_shutdown_cleanup_is_concurrent(synthetic_browser, monkeypatch):
    """Only scheduling proof: cleanup is deliberately substituted here."""
    owner = synthetic_browser.owner
    await owner._admit('a', lease_id=None)
    await owner._admit('b', lease_id=None)
    entered, release = asyncio.Event(), asyncio.Event()
    calls = []

    async def prove(runtime):
        calls.append(runtime)
        if len(calls) == 2:
            entered.set()
        await release.wait()
        await owner._remove_runtime(runtime)

    monkeypatch.setattr(owner, '_close_runtime', prove)
    task = asyncio.create_task(owner.shutdown())
    try:
        await asyncio.wait_for(entered.wait(), 1)
    finally:
        release.set()
        await task
    assert owner.live_count() == 0


async def test_shared_pid_guard_does_not_signal(synthetic_browser):
    fake = synthetic_browser
    await fake.owner.run('thread', ['snapshot'])
    first = fake.owner._sessions['thread'].record
    other = await fake.owner._admit('other', lease_id=None)
    # Corrupt in-memory ownership deliberately; this exercises the guard, not binding.
    other.record = replace(other.record, binding=first.binding)
    with pytest.raises(sessions.BrowserSessionCleanupError, match='shared'):
        await fake.owner.close_thread('thread')
    assert fake.children[first.session].poll() is None
    assert len(fake.calls) == 1


async def test_no_sessions_shutdown_never_calls_cli(synthetic_browser):
    await synthetic_browser.owner.shutdown()
    assert not synthetic_browser.calls


async def test_unlaunched_empty_session_can_retire(synthetic_browser):
    fake = synthetic_browser
    await fake.owner._admit('thread', lease_id=None)
    await fake.owner.close_thread('thread')
    assert fake.owner.live_count() == 0 and not fake.calls


@pytest.mark.parametrize('kwargs', [{'thread_id': '', 'args': ['snapshot']},
                                  {'thread_id': 'a', 'args': []},
                                  {'thread_id': 'a', 'args': ['close']},
                                  {'thread_id': 'a', 'args': ['snapshot'], 'timeout': 0}])
async def test_input_refusal_before_admission(synthetic_browser, kwargs):
    with pytest.raises(sessions.BrowserSessionError):
        await synthetic_browser.owner.run(**kwargs)
    assert not synthetic_browser.calls


def test_model_output_redacts_private_binding(synthetic_browser):
    owner = synthetic_browser.owner
    from host_access.browser_identity import DaemonBinding, BrowserSidecars
    binding = DaemonBinding(23456, 'birth', 'exe', BrowserSidecars(23456, 0, '0.27.0'))
    record = sessions.BrowserSessionRecord('raw-thread', 'opaque-session',
                                          lease_id='private-lease', binding=binding)
    result = owner._model_result(sessions.CommandResult(
        0, f'raw-thread opaque-session private-lease price=23456 {owner.socket_dir}', ''), record)
    assert result == ('[browser-thread] [browser-session] [browser-lease] '
                      'price=23456 [browser-runtime]')
    assert owner._model_result(sessions.CommandResult(1, 'secret', 'opaque-session 23456'), record) == (
        'Error: agent-browser command failed: [browser-session] 23456')


async def test_randomness_and_lease_binding(synthetic_browser, monkeypatch):
    owner = synthetic_browser.owner
    requested = []
    def opaque(count):
        requested.append(count)
        return 'opaque-session'
    monkeypatch.setattr(sessions.secrets, 'token_urlsafe', opaque)
    first = await owner._admit('thread', lease_id=None)
    assert requested == [12]
    assert first.record.session == 'opaque-session'
    assert await owner._admit('thread', lease_id='lease') is first
    with pytest.raises(sessions.BrowserSessionError, match='another lease'):
        await owner._admit('thread', lease_id='other')


async def test_timeout_fences_queued_effect_before_cleanup(synthetic_browser, monkeypatch):
    fake = synthetic_browser
    await fake.owner.run('thread', ['snapshot'])
    entered, release = asyncio.Event(), asyncio.Event()
    async def timed_out(argv, *, timeout):
        entered.set()
        await release.wait()
        return sessions.CommandResult(-1, '', '', timed_out=True)
    monkeypatch.setattr(fake.owner, '_invoke', timed_out)
    first = asyncio.create_task(fake.owner.run('thread', ['snapshot']))
    await entered.wait()
    queued = asyncio.create_task(fake.owner.run('thread', ['snapshot']))
    await asyncio.sleep(0)
    release.set()
    assert 'timed out' in await first
    with pytest.raises(sessions.BrowserSessionError):
        await queued
    assert fake.owner.live_count() == 0
    assert fake.owner.active_command_count() == 0


@pytest.mark.parametrize('wait_args', [['wait', '--load', 'networkidle'], ['wait', '#ready']])
async def test_timed_out_wait_keeps_the_session_running(synthetic_browser, wait_args):
    fake = synthetic_browser
    await fake.owner.run('thread', ['open', 'about:blank'])
    runtime = fake.owner._sessions['thread']
    fake.result = sessions.CommandResult(-1, '', '', timed_out=True)
    assert await fake.owner.run('thread', wait_args, timeout=15) == browser_cli.WAIT_TIMED_OUT
    assert runtime.record.terminal_state == 'running' and fake.owner.live_count() == 1
    fake.result = sessions.CommandResult(0, '', '')
    await fake.owner.run('thread', ['snapshot'])
    await fake.owner.shutdown()


async def test_interrupted_wait_resets_the_session(synthetic_browser):
    fake = synthetic_browser
    await fake.owner.run('thread', ['open', 'about:blank'])
    fake.result = sessions.CommandResult(-1, '', '', completion='interrupted')
    assert await fake.owner.run('thread', ['wait', '--load', 'networkidle']) != browser_cli.WAIT_TIMED_OUT
    assert fake.owner.live_count() == 0


async def test_timed_out_non_wait_command_still_resets_the_session(synthetic_browser):
    fake = synthetic_browser
    await fake.owner.run('thread', ['open', 'about:blank'])
    fake.result = sessions.CommandResult(-1, '', '', timed_out=True)
    assert 'timed out' in await fake.owner.run('thread', ['snapshot'])
    assert fake.owner.live_count() == 0
