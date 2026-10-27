from __future__ import annotations

import json

import pytest

from host_access import browser_control
from tools import browser_run_steps


@pytest.fixture
def tool():
    return browser_run_steps.BrowserRunStepsTool()


async def invoke(tool, monkeypatch, responses, **kwargs):
    calls = []

    async def fake_run(args, **run_kwargs):
        calls.append((args, run_kwargs))
        return responses.pop(0)

    monkeypatch.setattr(browser_run_steps, "_run", fake_run)
    result = await tool.execute("google-setup/console-project", _thread_id="thread", **kwargs)
    return json.loads(result), calls


def page_at(url, calls):
    async def fake_run(args, **_run_kwargs):
        calls.append(args)
        return "ok" if args[0] != "get" else ("Console" if args[1] == "title" else url)

    return fake_run


@pytest.mark.asyncio
async def test_runs_a_server_owned_file_successfully(tool, monkeypatch):
    calls = []
    monkeypatch.setattr(browser_run_steps, "_run", page_at("https://console.cloud.google.com/one", calls))

    result = json.loads(await tool.execute("google-setup/console-project", _thread_id="thread"))

    assert result == {"ok": True, "steps": ["project-page", "project-name", "project-create", "project-ready"]}
    assert calls[0] == ["open", "https://console.cloud.google.com/projectcreate"]


@pytest.mark.asyncio
async def test_project_resume_stops_before_api_enablement(tool, monkeypatch):
    calls = []
    monkeypatch.setattr(browser_run_steps, "_run", page_at("https://console.cloud.google.com/home", calls))

    result = json.loads(await tool.execute("google-setup/console-project", start_at="project-ready", _thread_id="thread"))

    assert result == {"ok": True, "steps": ["project-ready"]}
    assert not any(args[0] == "open" or args[0] == "click" for args in calls)


@pytest.mark.asyncio
async def test_off_domain_page_never_receives_a_type_or_click(tool, monkeypatch):
    calls = []
    monkeypatch.setattr(browser_run_steps, "_run", page_at("https://evil.example.test/login", calls))

    result = json.loads(await tool.execute("google-setup/console-client", start_at="client-name", _thread_id="thread"))

    assert result == {"ok": False, "step": "client-name", "says": "Name the client CORSIA.",
                      "saw": {"title": "Console", "url_path": "/login", "missing": 'role=textbox[name="Name"]'}}
    assert all(args[0] == "get" for args in calls)


@pytest.mark.asyncio
async def test_miss_returns_only_safe_shape_without_page_text(tool, monkeypatch):
    secret = "client_secret_should_not_escape"
    result, _calls = await invoke(tool, monkeypatch, ["Error: missing", "Console", "https://console.cloud.google.com/projectcreate"])

    assert result == {"ok": False, "step": "project-page", "says": "Open Google Cloud's project page.",
                      "saw": {"title": "Console", "url_path": "/projectcreate", "missing": "action"}}
    assert secret not in json.dumps(result)


@pytest.mark.asyncio
async def test_off_domain_stop_returns_safe_miss(tool, monkeypatch):
    result, _calls = await invoke(tool, monkeypatch, ["ok", "Elsewhere", "https://evil.example.test/path"])

    assert result == {"ok": False, "step": "project-page", "says": "Open Google Cloud's project page.",
                      "saw": {"title": "Elsewhere", "url_path": "/path", "missing": "action"}}


@pytest.mark.asyncio
@pytest.mark.parametrize("responses,message,start_at", [
    ([f"Error: {browser_control.PASSWORD_PAGE} Post nothing in chat."], browser_control.PASSWORD_PAGE + " Post nothing in chat.", "project-page"),
    ([f"Error: {browser_control.PERSON_STEP}"], browser_control.PERSON_STEP, "project-page"),
    (["ok", f"Error: {browser_control.PASSWORD_PAGE} Post nothing in chat."], browser_control.PASSWORD_PAGE + " Post nothing in chat.", "project-name"),
    ([f"Error: {browser_control.PERSON_STEP_DECLINED}"], browser_control.PERSON_STEP_DECLINED, "project-page"),
    (["ok", f"Error: {browser_control.PERSON_STEP_DECLINED}"], browser_control.PERSON_STEP_DECLINED, "project-name"),
    (["ok", "Console", f"Error: {browser_control.PERSON_STEP_DECLINED}"], browser_control.PERSON_STEP_DECLINED, "project-name"),
    ([f"Error: {browser_control.PASSWORD_DECLINED}"], browser_control.PASSWORD_DECLINED, "project-page"),
])
async def test_guard_refusals_keep_message_and_resume_point(tool, monkeypatch, responses, message, start_at):
    result, _calls = await invoke(tool, monkeypatch, responses)

    assert result["ok"] is False
    assert result["message"] == message
    assert result["step"] == result["start_at"] == start_at


@pytest.mark.asyncio
@pytest.mark.parametrize("start_at,expected", [("project-create", "project-ready"), ("project-ready", None)])
async def test_post_step_refusal_resumes_after_completed_step(tool, monkeypatch, start_at, expected):
    message = browser_control.PERSON_STEP
    responses = (["Console", "https://console.cloud.google.com/home"] if start_at == "project-create" else []) + ["ok", f"Error: {message}"]
    result, _calls = await invoke(tool, monkeypatch, responses, start_at=start_at)
    assert result["message"] == message
    if expected:
        assert result["start_at"] == expected and not result["ok"]
    else:
        assert result == {"ok": True, "steps": [start_at], "message": message}


@pytest.mark.asyncio
async def test_action_timeouts(tool, monkeypatch):
    calls = []
    async def fake_run(args, **kwargs):
        calls.append((args, kwargs))
        return "Console" if args == ["get", "title"] else "https://console.cloud.google.com/home" if args == ["get", "url"] else "ok"
    monkeypatch.setattr(browser_run_steps, "_run", fake_run)
    await tool.execute("google-setup/console-project", _thread_id="thread")
    await tool.execute("google-setup/console-client", start_at="download-wait", _thread_id="thread")
    commands = [(args, kwargs) for args, kwargs in calls if args[0] != "get"]
    for args, kwargs in commands:
        assert "timeout" not in kwargs
    assert {args[0] for args, _ in commands} >= {"open", "fill", "click", "wait"}


@pytest.mark.asyncio
@pytest.mark.parametrize("responses,times,success", [
    (["Error: missing", "Error: missing", "ok"], [0, 25, 50], True),
    (["Error: missing", "Error: missing", "Error: missing"], [0, 30, 60, 91], False),
])
async def test_selector_wait_retries_until_success_or_deadline(tool, monkeypatch, responses, times, success):
    calls = []
    clock = iter(times)
    from types import SimpleNamespace
    monkeypatch.setattr(browser_run_steps, "time", SimpleNamespace(monotonic=lambda: next(clock)))
    async def fake_run(args, **kwargs):
        calls.append((args, kwargs))
        if args[0] == "wait":
            return responses.pop(0)
        return "Console" if args[1] == "title" else "https://console.cloud.google.com/home"
    monkeypatch.setattr(browser_run_steps, "_run", fake_run)
    result = json.loads(await tool.execute("google-setup/console-project", start_at="project-ready", _thread_id="thread"))
    assert result["ok"] is success
    if not success:
        assert result["saw"]["missing"] == 'text="CORSIA"'
    waits = [(args, kwargs) for args, kwargs in calls if args[0] == "wait"]
    assert len(waits) == 3 and all("timeout" not in kwargs for _, kwargs in waits)


@pytest.mark.asyncio
async def test_start_at_resumes_from_named_step(tool, monkeypatch):
    calls = []

    async def fake_run(args, **run_kwargs):
        calls.append(args)
        return "ok" if args[0] not in {"get"} else ("Console" if args[1] == "title" else "https://console.cloud.google.com/auth/clients")

    monkeypatch.setattr(browser_run_steps, "_run", fake_run)
    result = json.loads(await tool.execute("google-setup/console-client", start_at="client-name", _thread_id="thread"))

    assert result["ok"] is True
    assert calls[2] == ["fill", 'role=textbox[name="Name"]', "CORSIA"]
    assert "clients-page" not in result["steps"]


@pytest.mark.asyncio
@pytest.mark.parametrize("name", ["unknown/file", "google-setup/../console-project", "google-setup/console-project.json"])
async def test_unknown_or_traversal_file_is_refused(tool, name):
    assert json.loads(await tool.execute(name, _thread_id="thread")) == {"ok": False, "error": "unknown steps file"}


def test_unknown_step_key_is_refused(tmp_path, monkeypatch):
    path = tmp_path / "example/steps"
    path.mkdir(parents=True)
    (path / "bad.json").write_text(json.dumps({"domains": ["google.com"], "steps": [
        {"id": "bad", "says": "Open", "action": "goto", "url": "https://console.cloud.google.com", "expected": "anything"}
    ]}), encoding="utf-8")
    monkeypatch.setattr(browser_run_steps, "_SKILLS", tmp_path)

    _data, error = browser_run_steps._load("example/bad")

    assert error == "unknown step key"


def test_shipped_step_files_load_and_validate():
    for name in ("google-setup/console-project", "google-setup/console-apis", "google-setup/console-client"):
        data, error = browser_run_steps._load(name)
        assert error is None and data is not None
        assert all(step["action"] in {"goto", "click", "type", "wait_for"} for step in data["steps"])
        assert not any("script" in step for step in data["steps"])
