"""Use mine changes nothing, and stopping management gives each model back exactly its own values.

Shares the fake oMLX of ``test_own_omlx_management``: every request the real
writers send is observed as oMLX would receive it.
"""

from __future__ import annotations

import asyncio

import pytest

from llm import local_setup_ownership, local_sources, omlx_global_settings, own_omlx_apply, own_omlx_management
from llm.local_model import LocalModel
from onboarding import setup_decisions
from tests.test_own_omlx_management import (  # noqa: F401
    APPLY, GLOBALS_BEFORE, MODEL, MODEL_BEFORE, OTHER, OTHER_BEFORE, SERVER, START, STOP, _fingerprint, _manage, omlx,
)
from tests.test_provider_sources import client  # noqa: F401

PICK_B = LocalModel("Jundot/Qwen3.8-9B-oQ4", 5_000_000_000, 32_768)
B_BEFORE = {"max_tokens": 2048, "max_context_window": 8192}


@pytest.fixture
def consent_marker(monkeypatch, omlx):  # noqa: F811
    monkeypatch.setattr("llm.local_setup_ownership.consent_marker_path",
                        lambda: own_omlx_management.record_path().with_name("consent"))


def _facts(monkeypatch) -> setup_decisions.Facts:
    monkeypatch.setattr("onboarding.project_discovery.session_apps", lambda _roots: [])
    monkeypatch.setattr(setup_decisions, "_saved_email", lambda: "")
    monkeypatch.setattr("api.provider_sources.list_sources", lambda: [{
        "source": "omlx", "omlx_management": local_sources.omlx_management(),
        "omlx_settings": {"differences": omlx_global_settings.differences(["srv"]), "fingerprint": _fingerprint()},
    }])
    return asyncio.run(setup_decisions.read_facts(probe_web_search=False))


@pytest.mark.parametrize("choice", [False, None])
def test_use_mine_is_told_apart_from_a_consent_that_never_showed_the_choice(
        client, omlx, consent_marker, monkeypatch, choice):  # noqa: F811
    local_setup_ownership.record_existing_omlx_consent(manage=choice)
    asyncio.run(omlx_global_settings.record_observation(SERVER))

    facts = _facts(monkeypatch)
    decision = setup_decisions.omlx_decision(facts)
    applied = client.post(APPLY, json={"fingerprint": _fingerprint()})

    if choice is False:
        assert facts.omlx_use_mine is True and decision is None
        assert (applied.status_code, applied.json()["detail"]) == (409, own_omlx_apply.USE_MINE)
        assert omlx.writes == []
    else:
        assert facts.omlx_use_mine is False and decision["title"] == "Change one oMLX setting for CORSIA?"
        assert applied.status_code == 200
        assert omlx.writes == [("POST", "/global-settings", {"max_concurrent_requests": 1})]
        assert own_omlx_management.read().originals_global == {"max_concurrent_requests": GLOBALS_BEFORE["max_concurrent_requests"]}
        assert client.post(STOP).status_code == 200
        assert {key: omlx.globals[key] for key in GLOBALS_BEFORE} == GLOBALS_BEFORE


def test_stopping_is_choosing_use_mine_until_the_person_lets_CORSIA_manage_it_again(
        client, omlx, consent_marker, monkeypatch):  # noqa: F811
    async def begin():
        return {"ok": True}

    monkeypatch.setattr("llm.local_setup.begin_local_setup", begin)
    _manage()
    assert client.post(STOP).status_code == 200
    omlx.requests.clear()

    assert own_omlx_management.uses_mine()
    assert client.post(APPLY, json={"fingerprint": _fingerprint()}).status_code == 409
    assert omlx.writes == []

    assert client.post(START).status_code == 200
    assert not own_omlx_management.uses_mine() and own_omlx_management.active()


def test_a_changed_pick_gives_each_model_back_its_own_values(client, omlx):  # noqa: F811
    _manage()
    assert client.post(APPLY, json={"fingerprint": _fingerprint()}).status_code == 200
    omlx.models[PICK_B.model] = dict(B_BEFORE)
    own_omlx_management.update(model=PICK_B)
    asyncio.run(omlx_global_settings.record_observation(SERVER))
    assert client.post(APPLY, json={"fingerprint": _fingerprint()}).status_code == 200
    originals = own_omlx_management.read().originals_models
    assert set(originals) == {MODEL.model, PICK_B.model}
    omlx.requests.clear()

    assert client.post(STOP).status_code == 200

    assert omlx.models == {MODEL.model: MODEL_BEFORE, PICK_B.model: B_BEFORE, OTHER: OTHER_BEFORE}
    restores = {path.split("/")[2]: body for method, path, body in omlx.writes if method == "PUT"}
    assert restores == originals


def test_a_model_the_person_removed_counts_as_restored(client, omlx):  # noqa: F811
    _manage()
    client.post(APPLY, json={"fingerprint": _fingerprint()})
    del omlx.models[MODEL.model]

    assert client.post(STOP).status_code == 200
    assert {key: omlx.globals[key] for key in GLOBALS_BEFORE} == GLOBALS_BEFORE
    assert own_omlx_management.read() is None and omlx.removed_keys == ["srv"]


def test_a_failed_model_restore_keeps_only_what_is_left_for_try_again(client, omlx):  # noqa: F811
    _manage()
    client.post(APPLY, json={"fingerprint": _fingerprint()})
    omlx.models[PICK_B.model] = dict(B_BEFORE)
    own_omlx_management.update(model=PICK_B)
    asyncio.run(omlx_global_settings.record_observation(SERVER))
    client.post(APPLY, json={"fingerprint": _fingerprint()})
    omlx.fail_models.add(PICK_B.model)

    failed = client.post(STOP)

    assert failed.status_code == 502
    record = own_omlx_management.read()
    assert record.restore_failed and record.originals_global == {} and set(record.originals_models) == {PICK_B.model}
    assert omlx.models[MODEL.model] == MODEL_BEFORE and omlx.removed_keys == []
    omlx.fail_models.clear()
    omlx.requests.clear()

    assert client.post(STOP).status_code == 200
    assert [path for _method, path, _body in omlx.writes] == [f"/models/{PICK_B.model}/settings"]
    assert omlx.models[PICK_B.model] == B_BEFORE and omlx.removed_keys == ["srv"]
