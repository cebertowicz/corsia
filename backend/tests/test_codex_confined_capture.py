"""Capture the full model tools array on the installed admitted Codex version.

CI captures the newest version. The digest test detects launch or tool surface
changes; recapture every listed version before updating the digest. No subscription,
API key, external service, or production state is used.
"""
from __future__ import annotations

import base64
import json
import shutil
import subprocess
from datetime import datetime, timezone

import pytest

from codex_confined_capture import AppServerClient, CaptureEndpoint, loopback_tls
from subagents import codex_confinement as confinement
from subagents.codex_app_server import CodexAppServerSession
from subagents.codex_app_server_events import to_dynamic_tool_specs


def model_catalog():
    # Adversarial metadata: all tool-producing selectors must be neutralized.
    return {"models": [{
        "slug": "confined-capture", "display_name": "Capture", "description": "fixture",
        "supported_reasoning_levels": [], "shell_type": "shell_command",
        "visibility": "list", "supported_in_api": True, "priority": 0,
        "base_instructions": "Review only the provided files.",
        "support_verbosity": False, "apply_patch_tool_type": "freeform",
        "truncation_policy": {"mode": "bytes", "limit": 10000},
        "experimental_supported_tools": ["clock", "request_user_input_async", "send_message_to_user_async"],
        "tool_mode": "code_mode_only", "multi_agent_version": "v2",
        "supports_search_tool": True, "use_responses_lite": True,
    }]}


def synthetic_id_token():
    claims = {"https://api.openai.com/auth": {
        "chatgpt_account_id": "synthetic-account", "chatgpt_plan_type": "pro",
    }}
    payload = base64.urlsafe_b64encode(json.dumps(claims).encode()).decode().rstrip("=")
    return f"e30.{payload}.synthetic"


def seed_home(tmp_path):
    source = tmp_path / "person-codex"
    source.mkdir()
    (source / "auth.json").write_text(json.dumps({
        "auth_mode": "chatgpt", "last_refresh": datetime.now(timezone.utc).isoformat(),
        "tokens": {
            "id_token": synthetic_id_token(), "access_token": "synthetic-test-only",
            "refresh_token": "synthetic-refresh-only", "account_id": "synthetic-account",
        },
    }))
    (source / "models_cache.json").write_text(json.dumps(model_catalog()))
    (source / "config.toml").write_text(
        'profile="hostile"\n[mcp_servers.hostile]\ncommand="must-never-run"\n'
        '[profiles.hostile]\nmodel="must-never-load"\nweb_search="live"\n'
    )
    (source / "AGENTS.md").write_text("PERSON_INSTRUCTION_MUST_NOT_LOAD")
    return source


def definitions():
    return tuple({"type": "function", "function": {
        "name": name, "description": "Read reviewed inputs",
        "parameters": {"type": "object", "properties": {"path": {"type": "string"}}},
    }} for name in sorted(confinement.UNTRUSTED_READ_TOOLS))


@pytest.mark.integration
@pytest.mark.asyncio
@pytest.mark.skipif(shutil.which("codex") is None, reason="optional installed Codex binary required")
async def test_pinned_binary_full_tools_cold_start_and_process_resume(tmp_path, monkeypatch):
    from infra import config

    monkeypatch.delenv("CODEX_ACCESS_TOKEN", raising=False)
    binary = shutil.which("codex")
    await confinement.verify_version(binary)
    monkeypatch.setattr(config, "RUNTIME_DIR", tmp_path / "runtime")
    staging = tmp_path / "attacker-repo" / "stage"
    staging.mkdir(parents=True)
    (staging.parent / ".git").mkdir()
    (staging.parent / "AGENTS.md").write_text("ATTACKER_PARENT_MUST_NOT_LOAD")
    (staging / "AGENTS.md").write_text("ATTACKER_STAGE_MUST_NOT_LOAD")
    source = seed_home(tmp_path)
    monkeypatch.setattr(confinement, "confined_dirs", lambda *_args, **_kw: (str(staging),))
    home = tmp_path / "isolated-codex"

    tls, certificate = loopback_tls(tmp_path)
    with CaptureEndpoint(tls) as endpoint:
        # login/src/auth/manager.rs:1727-1730,3001-3022: this non-JWT
        # access token with recent last_refresh does not proactively refresh. Trap
        # refresh/revoke and all other HTTP traffic locally regardless.
        for key, value in {
            "CODEX_CA_CERTIFICATE": str(certificate),
            "CODEX_REFRESH_TOKEN_URL_OVERRIDE": endpoint.url + "/oauth/token",
            "CODEX_REVOKE_TOKEN_URL_OVERRIDE": endpoint.url + "/oauth/revoke",
            "HTTP_PROXY": endpoint.url, "HTTPS_PROXY": endpoint.url, "ALL_PROXY": endpoint.url,
            "NO_PROXY": "127.0.0.1,localhost",
        }.items():
            monkeypatch.setenv(key, value)
        def session(provider_id=None):
            result = CodexAppServerSession(
                binary=binary, task="review", working_dir=str(staging), model="confined-capture",
                corsia_thread_id="worker", dynamic_tools=definitions(),
                codex_thread_id=provider_id, codex_home=str(home), source_codex_home=str(source),
                config_overrides=endpoint.config,
            )
            confinement.prepare_launch(result)
            assert result.confined and result.working_dir != str(staging)
            assert not any((tmp_path / "runtime" / "codex-confined-cwd" / "worker").iterdir())
            return result

        cold = session()
        params = {"model": cold.model, "cwd": cold.working_dir,
                  "sandbox": cold.sandbox_mode, "approvalPolicy": cold.approval_policy}
        async with AppServerClient(cold) as client:
            effective_config = await client.rpc("config/read", {"includeLayers": False})
            assert "hostile" not in json.dumps(effective_config)
            account = await client.rpc("account/read", {"refreshToken": False})
            assert account["account"]["type"] == "chatgpt"
            started = await client.rpc("thread/start", {
                **params, "environments": [], "dynamicTools": to_dynamic_tool_specs(definitions()),
            })
            provider_id = started["thread"]["id"]
            await client.complete_turn(provider_id)
        # Entire process is gone. Resume must restore persisted definitions only;
        # do not send dynamicTools, and keep native environment tools unavailable.
        async with AppServerClient(session(provider_id)) as client:
            await client.rpc("thread/resume", {**params, "threadId": provider_id, "excludeTurns": True})
            await client.complete_turn(provider_id)

    assert endpoint.unexpected == []  # Includes auth URLs and external proxy attempts.
    assert endpoint.chatgpt_requests == len(endpoint.requests) == 2
    assert endpoint.account_checks >= 2  # Real ChatGPT workspace routing, once per process.
    for request in endpoint.requests:
        confinement.assert_tools(request["tools"])
        # Equality of the full array also catches hosted web_search/image tools.
        assert request["tools"] == [{
            "type": "function", "name": d["function"]["name"],
            "description": d["function"]["description"], "strict": False,
            "parameters": d["function"]["parameters"],
        } for d in definitions()]
        captured = json.dumps(request)
        for marker in ("ATTACKER_STAGE_MUST_NOT_LOAD", "ATTACKER_PARENT_MUST_NOT_LOAD",
                       "PERSON_INSTRUCTION_MUST_NOT_LOAD", "must-never-run", "must-never-load",
                       "synthetic-test-only"):
            assert marker not in captured
    assert (home / "config.toml").read_text() == ""
    print("built-in openai, ChatGPT auth; unexpected/auth/proxy requests: []")
    print("loopback workspace checks:", endpoint.account_checks,
          "; optional local services:", sorted(set(endpoint.local_services)))
    version = subprocess.check_output([binary, "--version"], text=True).strip()
    print(f"{version} cold and process-resume full tools: " +
          json.dumps(endpoint.requests[0]["tools"], sort_keys=True))
