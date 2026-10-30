"""Saved sessions reject agent script entry points; disposable sessions retain them."""
import re
import shutil
import subprocess
import sys

import pytest

from host_access import browser_launch, browser_session_commands as commands
from host_access.browser_session_state import BrowserSessionError
from tests.browser_process_fixture import synthetic_browser  # noqa: F401


SCRIPT_REQUESTS = [
    ["eval", "document.cookie"], ["wait", "--fn", "document.cookie"],
    ["wait", "-f", "document.cookie"], ["addinitscript", "document.cookie"],
    ["open", "about:blank", "--restore-check-fn=1"],
    ["open", "about:blank", "--init-script=script.js"],
    ["open", "about:blank", "--extension=extension"],
    ["open", "about:blank", "--enable=react-devtools"],
    ["open", "about:blank", "--args=--load-extension=extension"],
    ["open", "java\nscript:alert(1)"], ["batch", '["eval document.cookie"]'],
    ["plugin", "run", "example"], ["mcp"],
]


@pytest.mark.parametrize("args", SCRIPT_REQUESTS)
async def test_saved_sessions_refuse_script_requests(synthetic_browser, monkeypatch, args):
    monkeypatch.setattr(browser_launch, "_owner_key", lambda _: "owner")
    message = "unavailable in a browser session|Browser navigation requires"
    if sys.platform == "linux" and args == ["open", "about:blank", "--args=--load-extension=extension"]:
        message = "Remove browser launch arguments from the command"
    with pytest.raises(BrowserSessionError, match=message):
        await synthetic_browser.owner.run("a", args)
    assert synthetic_browser.calls == []
    assert 'saved sign-in' in commands._SCRIPT_REFUSAL
    await synthetic_browser.owner.shutdown()


@pytest.mark.parametrize("args", [["eval", "1+1"], ["wait", "--fn", "true"]])
async def test_ephemeral_sessions_allow_scripts(synthetic_browser, monkeypatch, args):
    monkeypatch.setattr(browser_launch, "_owner_key", lambda _: None)
    await synthetic_browser.owner.run("a", ["open", "about:blank"])
    assert await synthetic_browser.owner.run("a", args) == "ok"
    await synthetic_browser.owner.shutdown()


async def test_production_open_allows_navigation_but_eval_and_javascript_url_are_refused(synthetic_browser, monkeypatch):
    from host_access.processes import ProcessOwner
    from tools import browser_runtime
    from tools.browser import BrowserOpenTool
    from tools.browser_actions import BrowserEvalTool

    monkeypatch.setattr(browser_launch, "_owner_key", lambda _: "owner")
    monkeypatch.setattr(browser_runtime, "_check_installed", lambda: None)
    process_owner = ProcessOwner.__new__(ProcessOwner)
    process_owner._browser_owner = synthetic_browser.owner
    monkeypatch.setattr(browser_runtime, "PROCESS_OWNER", process_owner)
    opened = await BrowserOpenTool().execute("https://example.com", wait_for_load=False, _thread_id="a")
    assert opened == "ok"
    assert "unavailable in a browser session" in await BrowserEvalTool().execute("document.cookie", _thread_id="a")
    refused = await BrowserOpenTool().execute("javascript:alert(1)", wait_for_load=False, _thread_id="a")
    assert "Browser navigation requires" in refused
    await synthetic_browser.owner.shutdown()


def test_saved_session_script_configuration_is_detected(monkeypatch):
    assert commands._script_config({"initScripts": ["agent.js"]})
    assert commands._script_config({"plugins": [{"capabilities": ["launch.mutate"]}]})
    assert commands._script_config({"extensions": ["extension"]})
    monkeypatch.setenv("AGENT_BROWSER_INIT_SCRIPTS", "agent.js")
    assert commands._script_config({})


def test_installed_cli_script_surface_is_enumerated():
    executable = shutil.which("agent-browser")
    if executable is None:
        pytest.skip("agent-browser is not installed")
    help_text = subprocess.run([executable, "--help"], capture_output=True, text=True, check=True).stdout
    reference = subprocess.run([executable, "skills", "get", "core", "--full"],
                               capture_output=True, text=True, check=True).stdout
    js_entries = set(re.findall(
        r"(?im)^\s*(eval|--[\w-]+)\b[^\n]*(?:<js>|javascript|js expression|init scripts?|extensions?)",
        help_text,
    ))
    assert js_entries == {"eval", "--restore-check-fn", "--init-script", "--extension", "--enable"}
    assert "agent-browser wait --fn" in reference and "(or -f)" in reference
    assert "agent-browser addinitscript <js>" in reference
    assert all(not commands._saved_command(request, pane=False) for request in SCRIPT_REQUESTS)
