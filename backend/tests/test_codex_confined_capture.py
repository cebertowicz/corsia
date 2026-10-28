"""Capture the FULL model tools array, including hosted tools, on the pinned CLI.

No subscription, API key, network provider, or production state is used. CI may
lack the optional Codex binary; a release claiming this version must run this
capture on its supported binary. See codex_confinement.CAPTURED_CODEX_VERSION.
"""
from __future__ import annotations

import json
import shutil

import pytest

from codex_confined_capture import AppServerClient, CaptureEndpoint
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


def seed_home(tmp_path):
    source = tmp_path / "person-codex"
    source.mkdir()
    (source / "auth.json").write_text(json.dumps({
        "auth_mode": "chatgpt", "tokens": {"access_token": "synthetic-test-only"},
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
    }} for name in sorted(confinement.REVIEW_READ_TOOLS))


@pytest.mark.integration
@pytest.mark.asyncio
@pytest.mark.skipif(shutil.which("codex") is None, reason="optional installed Codex binary required")
async def test_pinned_binary_full_tools_cold_start_and_process_resume(tmp_path, monkeypatch):
    from infra import config

    binary = shutil.which("codex")
    await confinement.verify_version(binary)
    monkeypatch.setattr(config, "RUNTIME_DIR", tmp_path / "runtime")
    staging = tmp_path / "attacker-repo" / "stage"
    staging.mkdir(parents=True)
    (staging.parent / ".git").mkdir()
    (staging.parent / "AGENTS.md").write_text("ATTACKER_PARENT_MUST_NOT_LOAD")
    (staging / "AGENTS.md").write_text("ATTACKER_STAGE_MUST_NOT_LOAD")
    source = seed_home(tmp_path)
    context = {"confinement": "review", "confined_read_dirs": json.dumps([str(staging)])}
    monkeypatch.setattr(confinement, "thread_context", lambda *_args, **_kw: context)
    home = tmp_path / "isolated-codex"

    with CaptureEndpoint() as endpoint:
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

    assert len(endpoint.requests) == 2
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
    print("codex-cli 0.162.1 cold and process-resume full tools: " +
          json.dumps(endpoint.requests[0]["tools"], sort_keys=True))
