"""`corsia providers` sends each subcommand to the right route, header-only (#2211)."""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))

import corsia_api  # noqa: E402
import providers_cli  # noqa: E402

_KEY = "endpoint-key-never-printed"
_SERVERS = [
    {"id": "ext-1", "instance_name": "Token Plan", "provider": "external", "kind": "external",
     "status": "online", "dialect": "anthropic", "has_key": True, "masked_key": "end…ted",
     "metadata": {"base_url": "https://plan.example/v1", "discovery": {"error": "no model list"}}},
    {"id": "omlx-1", "instance_name": "Studio", "provider": "omlx", "kind": "managed",
     "status": "online", "metadata": {}},
    {"id": "ext-old", "instance_name": "Gone", "kind": "external", "status": "removed", "metadata": {}},
]
_MODELS = [
    {"id": "claude-code/opus", "provider": "claude-code", "harness": "claude-code",
     "litellm_model": "claude-code/opus", "enabled": 1, "available": 1},
    {"id": "ext-9", "provider": "external", "harness": "native",
     "litellm_model": "external@ext-1/glm-5.3", "enabled": 0, "available": 1, "alias": "GLM"},
]


@pytest.fixture
def calls(monkeypatch):
    seen: list[dict] = []

    def _fake(method, base, path, cred, body=None, query=None, *, header_auth=False):
        seen.append({"method": method, "path": path, "body": body, "header_auth": header_auth})
        if path == "/api/settings/network-servers" and method == "GET":
            return {"servers": [dict(s) for s in _SERVERS]}
        if path == "/api/settings/models" and method == "GET":
            return {"models": [dict(m) for m in _MODELS]}
        if path == "/api/settings/providers":
            return {"providers": [{"name": "anthropic", "type": "cloud", "has_key": False}]}
        if path.endswith("/test"):
            return {"ok": False, "message": "Reached, but model discovery failed: no model list"}
        return {"ok": True, "server_id": "ext-2", "models": 2}

    monkeypatch.setattr(providers_cli, "_request", _fake)
    monkeypatch.setattr(providers_cli, "_resolve_token", lambda explicit: "agent-cred")
    return seen


def _writes(calls):
    return [c for c in calls if c["method"] != "GET"]


def test_every_call_is_header_only(calls):
    for argv in (["list"], ["models", "--all"], ["hide", "ext-9"], ["window", "ext-9", "262144"],
                 ["enable", "Token Plan"]):
        providers_cli.main(argv)
    assert calls and all(c["header_auth"] for c in calls)


def test_credential_is_never_taken_from_argv():
    with pytest.raises(SystemExit):
        providers_cli.main(["--" + "tok" + "en", "x", "list"])


def test_list_projects_status_and_discovery_error_without_keys(calls, capsys):
    providers_cli.main(["--json", "list"])
    out = capsys.readouterr().out
    data = json.loads(out)
    ids = [e["id"] for e in data["endpoints"]]
    assert ids == ["ext-1", "omlx-1"]  # a removed endpoint is not listed
    assert data["endpoints"][0]["discovery_error"] == "no model list"
    assert data["endpoints"][0]["has_key"] is True
    assert "masked_key" not in out and "…" not in out


def test_models_filters_by_endpoint_and_hides_hidden_by_default(calls, capsys):
    providers_cli.main(["--json", "models", "--provider", "ext-1"])
    assert json.loads(capsys.readouterr().out)["models"] == []
    providers_cli.main(["--json", "models", "--provider", "ext-1", "--all"])
    assert [m["id"] for m in json.loads(capsys.readouterr().out)["models"]] == ["ext-9"]


def test_models_text_names_each_model(calls, capsys):
    providers_cli.main(["models", "--all"])
    out = capsys.readouterr().out
    assert "GLM" in out and "hidden" in out and "claude-code/opus" in out


def test_add_sends_no_key(calls, capsys):
    providers_cli.main(["add", "--name", "Plan", "--base-url", "https://plan.example/v1",
                        "--dialect", "anthropic", "--model", "glm-5.3", "--model", "qwen3.8-flash"])
    (call,) = _writes(calls)
    assert call["method"] == "POST" and call["path"] == "/api/settings/network-servers"
    assert call["body"] == {"name": "Plan", "base_url": "https://plan.example/v1", "dialect": "anthropic",
                            "model_ids": ["glm-5.3", "qwen3.8-flash"]}


@pytest.mark.parametrize("flag", ["--key-stdin", "--key-env"])
def test_add_has_no_key_flags(calls, flag):
    argv = ["add", "--name", "P", "--base-url", "https://p.example/v1", flag]
    with pytest.raises(SystemExit) as exc:
        providers_cli.main(argv + (["X"] if flag == "--key-env" else []))
    assert exc.value.code == 2 and not _writes(calls)


def test_edit_by_name_keeps_the_stored_url_dialect_and_key(calls):
    providers_cli.main(["edit", "token plan", "--model", "glm-5.3"])
    (call,) = _writes(calls)
    assert call["method"] == "PUT" and call["path"] == "/api/settings/network-servers/ext-1"
    # The stored anthropic dialect is sent back, not the route's openai default.
    assert call["body"] == {"base_url": "https://plan.example/v1", "dialect": "anthropic",
                            "model_ids": ["glm-5.3"]}


def test_edit_clear_models_sends_an_empty_list(calls):
    providers_cli.main(["edit", "ext-1", "--clear-models"])
    assert _writes(calls)[0]["body"]["model_ids"] == []


def test_edit_refuses_a_managed_server(calls):
    with pytest.raises(SystemExit):
        providers_cli.main(["edit", "Studio", "--base-url", "https://x.example/v1"])
    assert not _writes(calls)


def test_unknown_endpoint_names_the_known_ones(calls, capsys):
    with pytest.raises(SystemExit) as exc:
        providers_cli.main(["remove", "nope"])
    assert exc.value.code == 2
    err = capsys.readouterr().err
    assert "Token Plan (ext-1)" in err and "Gone" not in err


@pytest.mark.parametrize(("argv", "method", "path", "body"), [
    (["remove", "ext-1"], "DELETE", "/api/settings/network-servers/ext-1", None),
    (["enable", "ext-1"], "PUT", "/api/settings/network-servers/ext-1/enabled", {"enabled": True}),
    (["disable", "Token Plan"], "PUT", "/api/settings/network-servers/ext-1/enabled", {"enabled": False}),
    (["show", "claude-code/opus"], "PATCH", "/api/settings/models/claude-code/opus", {"enabled": True}),
    (["hide", "ext-9"], "PATCH", "/api/settings/models/ext-9", {"enabled": False}),
    (["rename", "ext-9", "GLM 5.3"], "PATCH", "/api/settings/models/ext-9", {"alias": "GLM 5.3"}),
    (["rename", "ext-9", ""], "PATCH", "/api/settings/models/ext-9", {"alias": ""}),
    (["window", "claude-code/opus", "262_144"], "PATCH", "/api/models/claude-code/opus/profile",
     {"context_window": 262144}),
    (["window", "ext-9", "clear"], "PATCH", "/api/models/ext-9/profile", {"context_window": None}),
])
def test_write_routes(calls, argv, method, path, body):
    providers_cli.main(argv)
    (call,) = _writes(calls)
    assert (call["method"], call["path"], call["body"]) == (method, path, body)


def test_window_rejects_a_non_number(calls):
    with pytest.raises(SystemExit) as exc:
        providers_cli.main(["window", "ext-9", "big"])
    assert exc.value.code == 2 and not _writes(calls)


def test_failed_test_exits_nonzero_with_the_cause(calls, capsys):
    with pytest.raises(SystemExit) as exc:
        providers_cli.main(["test", "Token Plan"])
    assert exc.value.code == 1
    assert "model discovery failed: no model list" in capsys.readouterr().out


class _Resp:
    def __init__(self):
        self.body = b"{}"

    def read(self):
        return self.body

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


def test_header_auth_keeps_the_credential_out_of_the_url(monkeypatch):
    sent = {}

    def _urlopen(req, timeout):
        sent["url"], sent["auth"] = req.full_url, req.get_header("Authorization")
        return _Resp()

    monkeypatch.setattr(corsia_api.urllib.request, "urlopen", _urlopen)
    corsia_api.request("GET", "http://h", "/api/x", "secret-cred", header_auth=True)
    assert sent == {"url": "http://h/api/x", "auth": "Bearer secret-cred"}

    corsia_api.request("GET", "http://h", "/api/x", "secret-cred")
    assert "secret-cred" in sent["url"] and sent["auth"] is None  # other CLIs unchanged
