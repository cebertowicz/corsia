from __future__ import annotations

import json

import pytest

from host_access import browser_control
from tools import browser_steps


@pytest.fixture
def tool():
    return browser_steps.BrowserRunStepsTool()


async def invoke(tool, monkeypatch, responses, **kwargs):
    calls = []

    async def fake_run(args, **run_kwargs):
        calls.append((args, run_kwargs))
        return responses.pop(0)

    monkeypatch.setattr(browser_steps, "_run", fake_run)
    result = await tool.execute("google-setup/console-project", _thread_id="thread", **kwargs)
    return json.loads(result), calls


@pytest.mark.asyncio
async def test_runs_a_server_owned_file_successfully(tool, monkeypatch):
    responses = ["ok", "Console", "https://console.cloud.google.com/one"] * 6

    result, calls = await invoke(tool, monkeypatch, responses)

    assert result == {"ok": True, "steps": ["project-page", "project-name", "project-create", "project-ready", "apis-page", "apis-enable"]}
    assert calls[0][0] == ["open", "https://console.cloud.google.com/projectcreate"]


@pytest.mark.asyncio
async def test_miss_returns_only_safe_shape_without_page_text(tool, monkeypatch):
    secret = "client_secret_should_not_escape"
    result, _calls = await invoke(tool, monkeypatch, ["Error: missing", "Console", "https://console.cloud.google.com/projectcreate"], values={"address": secret})

    assert result == {"ok": False, "step": "project-page", "says": "Open Google Cloud's project page.",
                      "saw": {"title": "Console", "url_path": "/projectcreate", "missing": "action"}}
    assert secret not in json.dumps(result)


@pytest.mark.asyncio
async def test_off_domain_stop_returns_safe_miss(tool, monkeypatch):
    result, _calls = await invoke(tool, monkeypatch, ["ok", "Elsewhere", "https://evil.example.test/path"])

    assert result == {"ok": False, "step": "project-page", "says": "Open Google Cloud's project page.",
                      "saw": {"title": "Elsewhere", "url_path": "/path", "missing": "action"}}


@pytest.mark.asyncio
@pytest.mark.parametrize("responses,message", [
    ([f"Error: {browser_control.PASSWORD_PAGE} Post nothing in chat."], browser_control.PASSWORD_PAGE + " Post nothing in chat."),
    ([f"Error: {browser_control.PERSON_STEP}"], browser_control.PERSON_STEP),
    (["ok", f"Error: {browser_control.PASSWORD_PAGE} Post nothing in chat."], browser_control.PASSWORD_PAGE + " Post nothing in chat."),
])
async def test_guard_refusals_keep_message_and_resume_point(tool, monkeypatch, responses, message):
    result, _calls = await invoke(tool, monkeypatch, responses)

    assert result == {"ok": False, "step": "project-page", "says": "Open Google Cloud's project page.",
                      "message": message, "start_at": "project-page"}


@pytest.mark.asyncio
async def test_start_at_resumes_from_named_step(tool, monkeypatch):
    calls = []

    async def fake_run(args, **run_kwargs):
        calls.append(args)
        return "ok" if args[0] not in {"get"} else ("Console" if args[1] == "title" else "https://console.cloud.google.com/auth/clients")

    monkeypatch.setattr(browser_steps, "_run", fake_run)
    result = json.loads(await tool.execute("google-setup/console-client", start_at="client-name", _thread_id="thread"))

    assert result["ok"] is True
    assert calls[0] == ["fill", "label=Name", "CORSIA"]
    assert "clients-page" not in result["steps"]


@pytest.mark.asyncio
@pytest.mark.parametrize("name", ["unknown/file", "google-setup/../console-project", "google-setup/console-project.json"])
async def test_unknown_or_traversal_file_is_refused(tool, name):
    assert json.loads(await tool.execute(name, _thread_id="thread")) == {"ok": False, "error": "unknown steps file"}


def test_placeholder_outside_type_value_is_refused(tmp_path, monkeypatch):
    path = tmp_path / "example/steps"
    path.mkdir(parents=True)
    (path / "bad.json").write_text(json.dumps({"domains": ["google.com"], "steps": [
        {"id": "bad", "says": "${value}", "action": "goto", "url": "https://console.cloud.google.com"}
    ]}), encoding="utf-8")
    monkeypatch.setattr(browser_steps, "_SKILLS", tmp_path)

    _data, error = browser_steps._load("example/bad")

    assert error == "placeholders are allowed only in type values"


def test_shipped_step_files_load_and_validate():
    for name in ("google-setup/console-project", "google-setup/console-client"):
        data, error = browser_steps._load(name)
        assert error is None and data is not None
        assert all(step["action"] in {"goto", "click", "type", "wait_for"} for step in data["steps"])
