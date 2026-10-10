"""External image support is detected per model, and a user's choice sticks.

A generic model list reports no input types, so every External row used to say
text-only. Each model is now asked once, with a test image; rediscovery keeps
the answer, and a user's choice outranks detection.
"""

from __future__ import annotations

import asyncio
import base64
import json
import sqlite3
import struct
import zlib
from contextlib import closing

import pytest

from llm import external_image_input as eii
from llm.router import LLMResponse

_CAPS = {"supports_vision": 0, "supports_tools": 1, "supports_reasoning": 1}
_SID = "ns-plan"


def _seed(path, *model_ids: str) -> None:
    with closing(sqlite3.connect(str(path))) as db:
        db.execute(
            "INSERT INTO network_servers (id,provider,kind,dialect,instance_id,"
            "instance_name,port,status,last_seen_at,created_at,updated_at) "
            "VALUES (?,'external','external','openai',?,?,443,'online','t0','t0','t0')",
            (_SID, f"{_SID}.example.com:443/v1", _SID),
        )
        db.commit()
    _rediscover(list(model_ids))


def _rediscover(model_ids: list[str]) -> None:
    from llm import external_provider as ep

    server = {"id": _SID, "instance_name": _SID, "status": "online"}
    result = {"ok": False, "discovered": 0, "updated": 0, "missing": 0}
    ep._reconcile_external_models(_SID, server, "openai", model_ids, _CAPS, "t1", result)


def _row(path, pmid: str) -> tuple[int, dict]:
    with closing(sqlite3.connect(str(path))) as db:
        vision, meta = db.execute(
            "SELECT supports_vision, metadata_json FROM models WHERE litellm_model = ?",
            (f"external@{_SID}/{pmid}",),
        ).fetchone()
    return vision, json.loads(meta or "{}").get("image_input") or {}


def _answers(monkeypatch, replies: dict[str, LLMResponse]) -> list[dict]:
    """Route each probe to ``replies[served id]``; returns the calls made."""
    from llm import external_provider as ep
    from llm import router

    calls: list[dict] = []

    def params(server_id, pmid):
        return {"model": f"openai/{pmid}", "api_key": "k", "api_base": "https://x/v1"}

    async def completion(messages, tools=None, **kw):
        calls.append({"messages": messages, **kw})
        return replies[kw["model"].removeprefix("openai/")]

    monkeypatch.setattr(ep, "build_external_params", params)
    monkeypatch.setattr(router, "chat_completion", completion)
    return calls


def _said(text: str) -> LLMResponse:
    return LLMResponse(content=text)


def _failed(text: str) -> LLMResponse:
    return LLMResponse(content=f"Error calling LLM: {text}", finish_reason="error")


# ── the probe itself ─────────────────────────────────────────────────────────

def test_the_probe_is_one_text_part_and_one_real_green_png() -> None:
    (message,) = eii.probe_messages()
    text, image = message["content"]
    assert text["type"] == "text" and "colour" in text["text"]
    url = image["image_url"]["url"]
    assert url.startswith("data:image/png;base64,")
    png = base64.b64decode(url.split(",", 1)[1])
    assert png[:8] == b"\x89PNG\r\n\x1a\n"
    width, height = struct.unpack(">II", png[16:24])
    assert (width, height) == (64, 64)
    idat = png[png.index(b"IDAT") + 4:png.index(b"IEND") - 8]
    assert zlib.decompress(idat)[1:4] == b"\x00\xc8\x00"


@pytest.mark.parametrize("reply,failed,result", [
    ("Green.", False, "yes"),
    ("I can't see any image.", False, "no"),
    ("model does not support image input", True, "no"),
    ("Error calling LLM: This model does not support image input", True, "no"),
    ("Invalid content type image_url", True, "no"),
    ("Authentication failed for x. Check your API key", True, "unknown"),
    ("Rate limited by the API provider.", True, "unknown"),
])
def test_replies_are_classified(reply: str, failed: bool, result: str) -> None:
    got, detail = eii.classify_reply(reply, failed)
    assert got == result
    assert "Error calling LLM" not in detail


# ── detection, storage, and rediscovery ──────────────────────────────────────

def test_each_new_model_is_probed_once_and_the_answer_lands_on_the_row(
    isolated_db, monkeypatch,
) -> None:
    _seed(isolated_db, "deepseek-v4.1-flash", "glm-5.3")
    calls = _answers(monkeypatch, {
        "deepseek-v4.1-flash": _said("Green"),
        "glm-5.3": _failed("This model does not support image input"),
    })

    assert asyncio.run(eii.detect_image_input(_SID)) == 2
    assert _row(isolated_db, "deepseek-v4.1-flash")[0] == 1
    assert _row(isolated_db, "glm-5.3")[0] == 0
    assert _row(isolated_db, "glm-5.3")[1]["result"] == "no"
    assert all(c["retry_transient_errors"] is False for c in calls)

    # Already checked: a later discovery pass probes nothing.
    assert asyncio.run(eii.detect_image_input(_SID)) == 0
    assert len(calls) == 2


def test_rediscovery_keeps_the_detected_answer(isolated_db, monkeypatch) -> None:
    _seed(isolated_db, "deepseek-v4.1-flash")
    _answers(monkeypatch, {"deepseek-v4.1-flash": _said("green")})
    asyncio.run(eii.detect_image_input(_SID))

    _rediscover(["deepseek-v4.1-flash"])

    vision, state = _row(isolated_db, "deepseek-v4.1-flash")
    assert vision == 1 and state["source"] == "detected" and state["result"] == "yes"


def test_a_recheck_probes_each_detected_model_exactly_once(isolated_db, monkeypatch) -> None:
    _seed(isolated_db, "m")
    calls = _answers(monkeypatch, {"m": _said("green")})
    asyncio.run(eii.detect_image_input(_SID))

    eii.mark_for_recheck(_SID)
    # Until the new answer lands, the last one stays in force.
    assert _row(isolated_db, "m")[0] == 1
    assert asyncio.run(eii.detect_image_input(_SID)) == 1
    assert asyncio.run(eii.detect_image_input(_SID)) == 0

    assert len(calls) == 2
    assert "recheck" not in _row(isolated_db, "m")[1]


def test_an_inconclusive_probe_keeps_the_row_as_it_was(isolated_db, monkeypatch) -> None:
    _seed(isolated_db, "m")
    _answers(monkeypatch, {"m": _failed("Rate limited by the API provider.")})
    asyncio.run(eii.detect_image_input(_SID))

    vision, state = _row(isolated_db, "m")
    assert vision == 0 and state["result"] == "unknown"
    assert "could not finish" in state["detail"]


def test_a_user_choice_outranks_detection_and_survives_rediscovery(
    isolated_db, monkeypatch,
) -> None:
    _seed(isolated_db, "m")
    calls = _answers(monkeypatch, {"m": _said("I see nothing")})

    assert asyncio.run(eii.set_image_input(_SID, "m", "yes")) is True
    assert calls == []
    _rediscover(["m"])
    # Even a test that re-checks every model leaves the user's choice alone.
    eii.mark_for_recheck(_SID)
    assert asyncio.run(eii.detect_image_input(_SID)) == 0

    vision, state = _row(isolated_db, "m")
    assert vision == 1 and state["source"] == "user"


def test_choosing_detect_again_replaces_a_user_choice(isolated_db, monkeypatch) -> None:
    _seed(isolated_db, "m")
    _answers(monkeypatch, {"m": _said("I see nothing")})
    asyncio.run(eii.set_image_input(_SID, "m", "yes"))

    assert asyncio.run(eii.set_image_input(_SID, "m", "auto")) is True

    vision, state = _row(isolated_db, "m")
    assert vision == 0 and state["source"] == "detected" and state["result"] == "no"


def test_an_unknown_model_is_reported_not_created(isolated_db, monkeypatch) -> None:
    _seed(isolated_db, "m")
    assert asyncio.run(eii.set_image_input(_SID, "nope", "no")) is False


def test_discovery_of_an_external_endpoint_probes_its_unchecked_models(monkeypatch) -> None:
    from llm import external_provider as ep
    from llm import network_discovery as nd

    probed: list[str] = []

    async def discover(_sid):
        return {"ok": True}

    async def detect(sid, **_kw):
        probed.append(sid)
        return 1

    async def server(*_a, **_k):
        return {"id": _SID, "kind": "external"}

    monkeypatch.setattr(ep, "discover_external_models", discover)
    monkeypatch.setattr(eii, "detect_image_input", detect)
    monkeypatch.setattr(nd, "run_in_db", server)
    monkeypatch.setattr(nd, "_notify_models_changed_safe", lambda: None)

    asyncio.run(nd._discover_and_notify(_SID))

    assert probed == [_SID]


def test_the_route_pins_a_choice_and_refuses_an_unknown_model(isolated_db) -> None:
    from fastapi import HTTPException

    from api import network_servers as routes

    _seed(isolated_db, "m")

    ok = asyncio.run(routes.set_model_image_input(_SID, "m", routes.ImageInputBody(mode="no")))
    assert ok == {"ok": True}
    assert _row(isolated_db, "m")[1]["source"] == "user"
    with pytest.raises(HTTPException) as missing:
        asyncio.run(routes.set_model_image_input(_SID, "nope", routes.ImageInputBody(mode="no")))
    assert missing.value.status_code == 404


def test_the_summary_names_the_models_that_accept_images(isolated_db, monkeypatch) -> None:
    _seed(isolated_db, "a", "b", "c")
    _answers(monkeypatch, {
        "a": _said("green"), "b": _said("no image"), "c": _failed("timeout"),
    })
    asyncio.run(eii.detect_image_input(_SID))

    assert eii.image_input_summary(_SID) == "Images: a. Not determined: c."
