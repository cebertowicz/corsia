"""The app first-run warm is the reusable prefix of the selected model's turn."""

from __future__ import annotations

import asyncio
from copy import deepcopy
import json
import os
from pathlib import Path
import subprocess
from unittest.mock import AsyncMock, MagicMock

import pytest


SECRET_ENOUGH = "long-enough-passphrase"
MODEL = "omlx@srv-1/model-1"


def _write(sql: str, *args) -> None:
    from infra.database import write_transaction

    with write_transaction(None, scope="test_first_run_warm_parity") as db:
        db.execute(sql, args)


def _seed_omlx(monkeypatch) -> None:
    from llm import network_discovery

    server_id, _, provider_model = MODEL.partition("@")[2].partition("/")
    _write(
        "INSERT INTO network_servers (id,instance_id,instance_name,port,status,"
        "last_seen_at,created_at,updated_at) VALUES (?,?,'Test',9,'online','t','t','t')",
        server_id, server_id,
    )
    monkeypatch.setattr(network_discovery, "_LOADED_BY_SERVER", {server_id: {provider_model}})
    _write(
        "INSERT OR REPLACE INTO models (id,harness,provider,provider_model_id,"
        "litellm_model,display_name,backend,source,enabled,available,"
        "context_window,supports_reasoning,metadata_json,created_at,updated_at) VALUES "
        "(?,'native','omlx',?,?,?,'network','test',1,1,200000,1,'{}','t','t')",
        MODEL, provider_model, MODEL, provider_model,
    )
    _write(
        "INSERT OR REPLACE INTO model_profiles (model_id, context_window) "
        "VALUES (?, 200000)", MODEL,
    )


async def _until(condition, seconds: float = 10.0) -> None:
    for _ in range(int(seconds / 0.05)):
        if condition():
            return
        await asyncio.sleep(0.05)
    raise AssertionError("timed out waiting for the first turn's request")


@pytest.fixture
def app_first_run(monkeypatch, isolated_db, tmp_path):
    """Seed the production app landing plan and its held first-run wake."""
    from infra import accounts
    from infra.corsia_agent import get_corsia_agent
    from infra.database import write_transaction

    monkeypatch.setattr("infra.config.WORKSPACE", tmp_path / "workspace")
    # The common isolated DB seeds ordinary test accounts; this journey starts
    # before any account exists, exactly as the app's first-run path does.
    with write_transaction(None, scope="test_first_run_warm_parity") as db:
        db.execute("DELETE FROM auth_tokens")
        db.execute("DELETE FROM users")
    monkeypatch.setattr("onboarding.first_run_wake.app_reference_served", lambda: True)
    accounts.create_first_account("newcomer", SECRET_ENOUGH, first_run_path="app")
    conductor_id = get_corsia_agent()
    assert conductor_id
    _seed_omlx(monkeypatch)
    with write_transaction(None, scope="test_first_run_warm_parity") as db:
        db.execute(
            "UPDATE threads SET reasoning_effort='medium' WHERE id=?",
            (conductor_id,),
        )
    return conductor_id


@pytest.fixture
def wire(monkeypatch):
    """Record provider payloads while leaving endpoint and turn composition real."""
    import infra.state as state
    import llm.base as base
    import llm.resolution as resolution
    from llm.router import LLMResponse

    recorded = {"warm": [], "probe": [], "turn": []}
    provider = MagicMock()

    async def _probe(cfg, **kwargs):
        recorded["probe"].append(kwargs)
        return {"ok": False}

    async def _warm(cfg, **kwargs):
        recorded["warm"].append(kwargs)
        return {"ok": True}

    provider.probe_cache, provider.warm_cache = _probe, _warm
    monkeypatch.setattr("llm.providers.for_provider", lambda _name: provider)

    async def _completion(kwargs, **_rest):
        recorded["turn"].append(kwargs)
        return LLMResponse(content="hello", finish_reason="stop")

    async def _healthy(_model):
        return True, ""

    monkeypatch.setattr(base, "_completion_with_retries", _completion)
    monkeypatch.setattr(base, "_omlx_loaded_only_preflight", AsyncMock(return_value=None))
    monkeypatch.setattr(resolution, "_check_local_model_health", _healthy)
    monkeypatch.setattr(state, "broadcast_to_thread", MagicMock())
    monkeypatch.setattr(
        "llm.network_endpoints.build_api_base", lambda _server: "http://127.0.0.1:9/v1",
    )
    return recorded


async def _warm(conductor_id: str, wire, monkeypatch) -> dict:
    from api import thread_cache_warm as api_runtime
    from infra import state

    monkeypatch.setattr(api_runtime, "authenticate", lambda _token: {"id": "newcomer"})
    response = await api_runtime.warm_session_cache(conductor_id, token="test-token", model=MODEL)
    assert response["ok"] is True, response
    await state._active_warm_tasks[conductor_id]
    return wire["warm"][-1]


async def _first_turn(conductor_id: str, wire) -> dict:
    from infra.send_target import resolve_target
    from thread.model import switch_model

    target = await resolve_target(MODEL, thread_id=conductor_id)
    assert target.available, target.availability_reason
    outcome = await switch_model(
        conductor_id, MODEL, user_id="newcomer", actor_kind="user",
        actor_id="newcomer", reason="harness_model_switch",
    )
    assert outcome.changed
    await _until(lambda: wire["turn"])
    return wire["turn"][0]


def _normalized_input(request: dict) -> dict:
    """The provider boundary input whose size this diagnostic compares."""
    return {"messages": request["messages"], "tools": request["tools"]}


def _input_bytes(request: dict) -> bytes:
    """Preserve captured key and list order when checking reusable input bytes."""
    return json.dumps(
        _normalized_input(request), ensure_ascii=False, separators=(",", ":"),
    ).encode("utf-8")


def _input_tokens(request: dict) -> int:
    """Estimate a normalized boundary input with cl100k_base, not oMLX's tokenizer."""
    import tiktoken

    payload = json.dumps(
        _normalized_input(request), ensure_ascii=False, sort_keys=True,
        separators=(",", ":"), default=str,
    )
    return len(tiktoken.get_encoding("cl100k_base").encode(payload, disallowed_special=()))


def _text_tokens(text: str) -> int:
    import tiktoken

    return len(tiktoken.get_encoding("cl100k_base").encode(text, disallowed_special=()))


def _baseline_instruction(reference: str, path: str) -> str:
    """Read an explicitly requested historical resource for local diagnostics."""
    repository = Path(__file__).resolve().parents[2]
    result = subprocess.run(
        ["git", "show", f"{reference}:backend/prompts/system/v1/{path}"],
        cwd=repository, check=True, capture_output=True, text=True,
    )
    return result.stdout.strip()


def _reconstructed_baseline_input(current_input: str, reference: str) -> str:
    """Replace only the onboarding notice inside the captured delivery aggregate."""
    event_start = current_input.index("[EVENT: onboarding.first_run]")
    setup_marker = "\n\n## Setup facts\n\n"
    header_end = current_input.index("\n", current_input.index("\n", event_start) + 1) + 1
    setup_start = current_input.index(setup_marker, event_start)
    baseline_instruction = "\n\n".join((
        _baseline_instruction(reference, "skills/conductor-bootstrap/references/first-run.md"),
        _baseline_instruction(reference, "skills/conductor-bootstrap/references/first-run-app.md"),
    ))
    return current_input[:header_end] + baseline_instruction + current_input[setup_start:]


def _with_final_input(request: dict, input_content: str) -> dict:
    """Copy the captured real request and alter only its final user input."""
    reconstructed = deepcopy(request)
    assert reconstructed["messages"][-1]["role"] == "user"
    reconstructed["messages"][-1]["content"] = input_content
    return reconstructed


def _model_and_account_binding(thread_id: str) -> tuple[object, object | None]:
    """Return the thread model and its selected vendor account, if either exists."""
    from infra.database import get_db

    db = get_db()
    try:
        model = db.execute("SELECT model FROM threads WHERE id=?", (thread_id,)).fetchone()["model"]
        account = db.execute(
            "SELECT account_id FROM thread_vendor_accounts WHERE thread_id=?", (thread_id,),
        ).fetchone()
        return model, account["account_id"] if account is not None else None
    finally:
        db.close()


@pytest.mark.asyncio
async def test_app_first_run_warm_is_the_first_selected_turn_prefix(app_first_run, wire, monkeypatch):
    from infra.database import get_db

    db = get_db()
    try:
        assert db.execute(
            "SELECT 1 FROM messages WHERE thread_id=? AND id=?",
            (app_first_run, f"first-run-opening:{app_first_run}"),
        ).fetchone() is None
        assert db.execute("SELECT 1 FROM plan_nodes WHERE workflow_ref='first-run-app'").fetchone() is not None
        held_wake_before_selection = db.execute(
            "SELECT content FROM messages WHERE thread_id=? AND delivery_state='queued'",
            (app_first_run,),
        ).fetchone()["content"]
    finally:
        db.close()

    # Warming an unbound thread must not choose a model or account or publish
    # the greeting. The setup plan is already visible before the model pick.
    assert _model_and_account_binding(app_first_run) == (None, None)
    warm = await _warm(app_first_run, wire, monkeypatch)
    assert _model_and_account_binding(app_first_run) == (None, None)
    db = get_db()
    try:
        assert db.execute(
            "SELECT 1 FROM messages WHERE thread_id=? AND id=?",
            (app_first_run, f"first-run-opening:{app_first_run}"),
        ).fetchone() is None
        assert db.execute("SELECT 1 FROM plan_nodes WHERE workflow_ref='first-run-app'").fetchone() is not None
    finally:
        db.close()
    turn = await _first_turn(app_first_run, wire)

    from infra.database import get_db
    from infra.onboarding_events import first_run_message_id

    db = get_db()
    try:
        greeting = db.execute(
            "SELECT content FROM messages WHERE thread_id=? AND id=?",
            (app_first_run, f"first-run-opening:{app_first_run}"),
        ).fetchone()["content"]
        first_run_wake = db.execute(
            "SELECT content FROM messages WHERE thread_id=? AND id=?",
            (app_first_run, first_run_message_id(app_first_run)),
        ).fetchone()["content"]
    finally:
        db.close()
    assert first_run_wake != held_wake_before_selection

    # The seeded greeting is history and therefore cacheable. The selected
    # provider receives the actual FIFO aggregate, keeping the passive context
    # digest and the first-run wake together as current input.
    assert {"role": "assistant", "content": greeting} in warm["messages"]
    assert turn["messages"][: len(warm["messages"])] == warm["messages"]
    wake = turn["messages"][-1]["content"]
    assert turn["messages"][-1]["role"] == "user"
    assert wake.startswith("CORSIA is delivering 2 queued inbound inputs together.")
    first_member, second_member = wake.split("\n\n[2/2]\n", maxsplit=1)
    first_summary = " | ".join(first_member.splitlines()[3:5])
    second_summary = " | ".join(second_member.splitlines()[:2])
    print(f"first-run aggregate inputs: first={first_summary}; second={second_summary}")
    assert len(turn["messages"]) == len(warm["messages"]) + 1
    assert turn["tools"] == warm["tools"]
    assert _input_bytes({
        "messages": turn["messages"][:-1], "tools": turn["tools"],
    }) == _input_bytes(warm)
    assert {key: value for key, value in turn["extra_body"].items() if key != "session_id"} == {
        key: value for key, value in warm.items() if key not in ("model_id", "messages", "tools")
    }
    assert warm["reasoning_effort"] == "medium"
    assert turn["extra_body"]["reasoning_effort"] == "medium"

    # Diagnostic only. The warm and current paths are the captured provider
    # requests. The reconstructed baseline retains every current request field
    # except the final inline instruction in the first-run input.
    warm_tokens = _input_tokens(warm)
    first_turn_tokens = _input_tokens(turn)
    reusable_only_tokens = _input_tokens({
        "messages": turn["messages"][:len(warm["messages"])],
        "tools": turn["tools"],
    })
    assert reusable_only_tokens == warm_tokens
    assert warm_tokens < first_turn_tokens
    print(
        "first-run cl100k normalized boundary-input tokens (estimate only): "
        f"warm_reusable_prefix={warm_tokens} "
        f"current_first_turn_full={first_turn_tokens} "
        f"current_beyond_prefix={first_turn_tokens - reusable_only_tokens} "
        f"final_wake_content={_text_tokens(wake)}"
    )

    # Historical comparison is an explicit local diagnostic. CI has only a
    # shallow checkout and HEAD advances with this test, so it is never proof.
    baseline_reference = os.environ.get("FIRST_RUN_BASELINE_REF")
    if baseline_reference:
        baseline_turn = _with_final_input(
            turn, _reconstructed_baseline_input(wake, baseline_reference),
        )
        baseline_turn_tokens = _input_tokens(baseline_turn)
        baseline_reusable_only_tokens = _input_tokens({
            "messages": baseline_turn["messages"][:len(warm["messages"])],
            "tools": baseline_turn["tools"],
        })
        assert baseline_reusable_only_tokens == warm_tokens
        assert baseline_turn_tokens > first_turn_tokens
        print(
            "first-run reconstructed baseline cl100k normalized boundary-input tokens "
            "(estimate only): "
            f"ref={baseline_reference} full={baseline_turn_tokens} "
            f"beyond_prefix={baseline_turn_tokens - baseline_reusable_only_tokens}"
        )
