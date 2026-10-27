"""The guard and its command must observe a consistent command ordering."""
import asyncio
from types import SimpleNamespace

import pytest

from host_access import browser_cdp, browser_cli, browser_control, browser_pane_state, browser_session_commands
from host_access.browser_session_state import BrowserRuntime, BrowserSessionRecord


@pytest.mark.asyncio
async def test_pane_navigation_cannot_overtake_guarded_command(monkeypatch):
    runtime = BrowserRuntime(BrowserSessionRecord("thread", "session", binding=object()))
    guard_checked = asyncio.Event()
    release_guard = asyncio.Event()
    pane_admitted = asyncio.Event()
    calls = []

    class Owner:
        _queued_actions = 0

        async def _pin_native(self):
            pass

        async def _admit(self, *_args, **_kwargs):
            if guard_checked.is_set():
                pane_admitted.set()
            return runtime

        async def _run_admitted(self, _runtime, args, **_kwargs):
            calls.append(tuple(args))
            return SimpleNamespace(timed_out=False), False

        def _model_result(self, _result, _record):
            return "ok"

    async def endpoint(_command):
        return "ws://page"

    async def guard(*_args, **_kwargs):
        calls.append("guard")
        guard_checked.set()
        await release_guard.wait()

    async def finished(*_args):
        pass

    monkeypatch.setattr(browser_cli, "check_local_mode", lambda _args: {})
    monkeypatch.setattr(browser_cdp, "page_endpoint", endpoint)
    monkeypatch.setattr(browser_control, "require_page_access", guard)
    monkeypatch.setattr(browser_pane_state, "has_pane", lambda _thread: False)
    monkeypatch.setattr(browser_pane_state, "agent_acted", lambda _thread: None)
    monkeypatch.setattr(browser_pane_state, "page_command_starting", lambda *_args: None)
    monkeypatch.setattr(browser_pane_state, "page_command_finished", finished)

    owner = Owner()
    async def run(args, pane=False):
        return await browser_session_commands.run(owner, "thread", args, lease_id=None, timeout=1,
                                                  show_window=None, session="existing", pane=pane)

    agent = asyncio.create_task(run(("snapshot",)))
    await guard_checked.wait()
    pane = asyncio.create_task(run(("open", "https://password.example"), pane=True))
    await pane_admitted.wait()
    # The admitted pane has reached the competing command path before the guard resumes.
    await asyncio.sleep(0)
    release_guard.set()
    await asyncio.gather(agent, pane)
    assert calls == ["guard", ("snapshot",), ("open", "https://password.example")]
