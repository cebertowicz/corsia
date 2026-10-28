from __future__ import annotations

import json

import pytest

from host_access import browser_control
from host_access.browser_sessions import BrowserSessionCleanupError, BrowserSessionError, BrowserSetupError
from tools import browser_run_steps, browser_runtime


@pytest.fixture
def tool():
    return browser_run_steps.BrowserRunStepsTool()


async def invoke(tool, monkeypatch, responses, **kwargs):
    calls = []

    async def fake_run(args, **run_kwargs):
        calls.append((args, run_kwargs))
        return responses.pop(0)

    monkeypatch.setattr(browser_run_steps, "_call", fake_run)
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
    monkeypatch.setattr(browser_run_steps, "_call", page_at("https://console.cloud.google.com/one", calls))

    result = json.loads(await tool.execute("google-setup/console-project", _thread_id="thread"))

    assert result == {"ok": True, "steps": ["project-page", "project-name", "project-create", "project-ready"]}
    assert calls[0] == ["open", "https://console.cloud.google.com/projectcreate"]


@pytest.mark.asyncio
async def test_project_resume_stops_before_api_enablement(tool, monkeypatch):
    calls = []
    monkeypatch.setattr(browser_run_steps, "_call", page_at("https://console.cloud.google.com/home", calls))

    result = json.loads(await tool.execute("google-setup/console-project", start_at="project-ready", _thread_id="thread"))

    assert result == {"ok": True, "steps": ["project-ready"]}
    assert not any(args[0] == "open" or args[0] == "click" for args in calls)


@pytest.mark.asyncio
async def test_off_domain_page_never_receives_a_type_or_click(tool, monkeypatch):
    calls = []
    monkeypatch.setattr(browser_run_steps, "_call", page_at("https://evil.example.test/login", calls))

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
@pytest.mark.parametrize("error,message", [
    (BrowserSetupError("agent-browser is missing"), BrowserSetupError("agent-browser is missing").tool_message),
    (BrowserSessionCleanupError("unverified"), BrowserSessionCleanupError("unverified").tool_message),
    (BrowserSessionCleanupError("pending", recovery="pending"), BrowserSessionCleanupError("pending", recovery="pending").tool_message),
    (BrowserSessionError("the browser is no longer open"), "Error: the browser is no longer open"),
    (Exception("failure"), f"Error: {browser_runtime.COMMAND_FAILED}"),
])
@pytest.mark.parametrize("before,start_at", [
    ([], None),
    (["ok"], None),
    (["ok", "Console"], None),
    (["Error: missing"], None),
    (["Error: missing", "Console"], None),
    ([], "project-ready"),
    ([], "project-name"),
    (["Console"], "project-name"),
])
async def test_owner_error_stops_at_each_command_or_read(tool, monkeypatch, error, message, before, start_at):
    responses = list(before)
    calls = []
    monkeypatch.setattr(browser_runtime, "_check_installed", lambda: None)
    async def run_browser(thread_id, args, **kwargs):
        calls.append(args)
        if responses:
            return responses.pop(0)
        raise error
    monkeypatch.setattr(browser_runtime.PROCESS_OWNER, "run_browser", run_browser)
    result = json.loads(await tool.execute("google-setup/console-project", start_at=start_at, _thread_id="thread"))
    assert result == {"ok": False, "error": message.removeprefix("Error: ")}
    assert len(calls) == len(before) + 1


@pytest.mark.asyncio
async def test_wait_does_not_retry_browser_session_error(tool, monkeypatch):
    calls = []
    monkeypatch.setattr(browser_runtime, "_check_installed", lambda: None)
    async def run_browser(thread_id, args, **kwargs):
        calls.append(args)
        raise BrowserSessionError("the browser is no longer open")
    monkeypatch.setattr(browser_runtime.PROCESS_OWNER, "run_browser", run_browser)
    result = json.loads(await tool.execute("google-setup/console-project", start_at="project-ready", _thread_id="thread"))
    assert result == {"ok": False, "error": "the browser is no longer open"}
    assert len(calls) == 1 and calls[0][0] == "wait"


@pytest.mark.asyncio
async def test_raised_guard_refusal_keeps_resume_point(tool, monkeypatch):
    monkeypatch.setattr(browser_runtime, "_check_installed", lambda: None)
    async def run_browser(*args, **kwargs):
        raise BrowserSessionError(browser_control.PASSWORD_PAGE)
    monkeypatch.setattr(browser_runtime.PROCESS_OWNER, "run_browser", run_browser)
    result = json.loads(await tool.execute("google-setup/console-project", _thread_id="thread"))
    assert result["message"] == browser_control.PASSWORD_PAGE
    assert result["start_at"] == "project-page" and not result["ok"]


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
    monkeypatch.setattr(browser_run_steps, "_call", fake_run)
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
    pauses = []

    async def fake_sleep(seconds):
        pauses.append(seconds)
    monkeypatch.setattr(browser_run_steps, "asyncio", SimpleNamespace(sleep=fake_sleep))
    async def fake_run(args, **kwargs):
        calls.append((args, kwargs))
        if args[0] == "wait":
            return responses.pop(0)
        return "Console" if args[1] == "title" else "https://console.cloud.google.com/home"
    monkeypatch.setattr(browser_run_steps, "_call", fake_run)
    result = json.loads(await tool.execute("google-setup/console-project", start_at="project-ready", _thread_id="thread"))
    assert result["ok"] is success
    if not success:
        assert result["saw"]["missing"] == 'text="CORSIA"'
    waits = [(args, kwargs) for args, kwargs in calls if args[0] == "wait"]
    assert len(waits) == 3 and all("timeout" not in kwargs for _, kwargs in waits)
    assert pauses == [browser_run_steps._RETRY_PAUSE_S] * 2  # a pause between tries, none after the last


@pytest.mark.asyncio
async def test_start_at_resumes_from_named_step(tool, monkeypatch):
    calls = []

    async def fake_run(args, **run_kwargs):
        calls.append(args)
        return "ok" if args[0] not in {"get"} else ("Console" if args[1] == "title" else "https://console.cloud.google.com/auth/clients")

    monkeypatch.setattr(browser_run_steps, "_call", fake_run)
    result = json.loads(await tool.execute("google-setup/console-client", start_at="client-name", _thread_id="thread"))

    assert result["ok"] is True
    assert calls[2] == ["fill", 'role=textbox[name="Name"]', "CORSIA"]
    assert "clients-page" not in result["steps"]


@pytest.mark.asyncio
@pytest.mark.parametrize("name", ["unknown/file", "google-setup/../console-project", "google-setup/console-project.json", "/google-setup/console-project", "../file"])
async def test_unknown_or_traversal_file_is_refused(tool, name):
    assert json.loads(await tool.execute(name, _thread_id="thread")) == {"ok": False, "error": "unknown steps file"}


def installed_skill(tmp_path, monkeypatch):
    monkeypatch.setattr(browser_run_steps.config, "WORKSPACE", tmp_path)
    folder = tmp_path / "skills/example"
    folder.mkdir(parents=True)
    (folder / "SKILL.md").write_text("---\nname: example\ndescription: Use for example setup.\n---\nSetup.\n", encoding="utf-8")
    steps = folder / "steps"
    steps.mkdir()
    return steps


def test_unknown_step_key_is_refused(tmp_path, monkeypatch):
    path = installed_skill(tmp_path, monkeypatch)
    (path / "bad.json").write_text(json.dumps({"domains": ["google.com"], "steps": [
        {"id": "bad", "says": "Open", "action": "goto", "url": "https://console.cloud.google.com", "expected": "anything"}
    ]}), encoding="utf-8")

    _data, error = browser_run_steps._load("example/bad", "thread", None)

    assert error == "unknown step key"


def test_shipped_step_files_load_and_validate():
    for name in ("google-setup/console-project", "google-setup/console-apis", "google-setup/console-client"):
        data, error = browser_run_steps._load(name, "thread", None)
        assert error is None and data is not None
        assert all(step["action"] in {"goto", "click", "type", "wait_for"} for step in data["steps"])
        assert not any("script" in step for step in data["steps"])


def test_installed_skill_rejects_escaped_step_symlink(tmp_path, monkeypatch):
    steps = installed_skill(tmp_path, monkeypatch)
    outside = tmp_path / "outside.json"
    outside.write_text('{"domains":["example.com"],"steps":[]}', encoding="utf-8")
    (steps / "escape.json").symlink_to(outside)
    assert browser_run_steps._load("example/escape", "thread", None) == (None, "unknown steps file")


def test_installed_skill_rejects_escaped_steps_directory(tmp_path, monkeypatch):
    steps = installed_skill(tmp_path, monkeypatch)
    steps.rmdir()
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "run.json").write_text('{"domains":["example.com"],"steps":[]}', encoding="utf-8")
    steps.symlink_to(outside, target_is_directory=True)
    assert browser_run_steps._load("example/run", "thread", None) == (None, "unknown steps file")


def test_system_skill_shadows_installed_name(tmp_path, monkeypatch):
    monkeypatch.setattr(browser_run_steps.config, "WORKSPACE", tmp_path)
    folder = tmp_path / "skills/google-setup"
    (folder / "steps").mkdir(parents=True)
    (folder / "SKILL.md").write_text("---\nname: google-setup\ndescription: Use for setup.\n---\n", encoding="utf-8")
    (folder / "steps/console-project.json").write_text('{"domains":["example.com"],"steps":[]}', encoding="utf-8")
    data, error = browser_run_steps._load("google-setup/console-project", "thread", None)
    assert error is None and "google.com" in data["domains"]


def test_project_and_thread_skills_follow_effective_precedence(tmp_path, monkeypatch):
    from tests.project_ref_helpers import project_ref, select_workspace
    from tests.test_skills_registry import _db

    workspace = tmp_path / "workspace"
    project = workspace / "projects/corsia"
    project.mkdir(parents=True)
    select_workspace(monkeypatch, workspace)
    monkeypatch.setattr(browser_run_steps.config, "WORKSPACE", workspace)
    for root, marker in ((project, "project"), (project / ".threads/thread", "thread")):
        folder = root / "skills/example"
        (folder / "steps").mkdir(parents=True)
        (folder / "SKILL.md").write_text("---\nname: example\ndescription: Use for setup.\n---\n", encoding="utf-8")
        (folder / "steps/run.json").write_text(json.dumps({"domains": ["example.com"], "steps": [
            {"id": marker, "says": marker, "action": "click", "selector": "#go"}
        ]}), encoding="utf-8")

    def db():
        conn = _db()
        conn.execute("INSERT INTO projects (id, name, path) VALUES (?, ?, ?)", ("proj", "CORSIA", project_ref("corsia")))
        conn.execute("INSERT INTO threads (id, project_id) VALUES (?, ?)", ("thread", "proj"))
        return conn

    monkeypatch.setattr(browser_run_steps, "get_db", db)
    data, error = browser_run_steps._load("example/run", "thread", "proj")
    assert error is None and data["steps"][0]["id"] == "thread"
    (project / ".threads/thread/skills/example/SKILL.md").unlink()
    data, error = browser_run_steps._load("example/run", "thread", "proj")
    assert error is None and data["steps"][0]["id"] == "project"


def test_installed_skill_rejects_script_and_reference(tmp_path, monkeypatch):
    steps = installed_skill(tmp_path, monkeypatch)
    script = {"domains": ["example.com"], "steps": [{"id": "x", "says": "Click", "action": "click", "script": "alert(1)"}]}
    (steps / "script.json").write_text(json.dumps(script), encoding="utf-8")
    assert browser_run_steps._load("example/script", "thread", None)[1] == "unknown step key"
    script["include"] = "other/file"
    (steps / "reference.json").write_text(json.dumps(script), encoding="utf-8")
    assert browser_run_steps._load("example/reference", "thread", None)[1] == "steps file must contain domains and steps"


@pytest.mark.asyncio
async def test_installed_skill_runs_and_logs_origin(tmp_path, monkeypatch):
    steps = installed_skill(tmp_path, monkeypatch)
    (steps.parent / "corsia.yaml").write_text("origin:\n  source: example\n  version: '2.1'\n", encoding="utf-8")
    (steps / "run.json").write_text(json.dumps({"domains": ["example.com"], "steps": [
        {"id": "click", "says": "Click", "action": "click", "selector": "#go"}
    ]}), encoding="utf-8")
    calls = []
    monkeypatch.setattr(browser_run_steps, "_call", page_at("https://example.com/", calls))
    messages = []
    sink = browser_run_steps.logger.add(messages.append, format="{message}", level="INFO")
    try:
        result = json.loads(await browser_run_steps.BrowserRunStepsTool().execute("example/run", _thread_id="thread"))
    finally:
        browser_run_steps.logger.remove(sink)
    assert result == {"ok": True, "steps": ["click"]}
    assert ["click", "#go"] in calls
    assert any("source=installed skill=example version=2.1" in str(message) for message in messages)
