"""Send admission uses the local model readiness contract."""

from dataclasses import replace

import pytest


def _not_loaded_target():
    from llm.resolution import resolve_execution_target
    from tests.unavailable_model_support import seed_model

    seed_model("omlx@srv/m")
    return replace(resolve_execution_target("omlx@srv/m"), available=False, availability_reason="model_not_loaded")


@pytest.mark.parametrize("state", ["no_room", "no_memory", "unreachable"])
def test_unready_local_target_fails_a_person_send(isolated_db, monkeypatch, state):
    from infra.send_target import classify_target_admission, person_send_target
    from llm.local_model_load import Readiness

    target = _not_loaded_target()
    monkeypatch.setattr("llm.local_model_load.readiness", lambda *_a, **_k: Readiness(state, 1))
    actual = person_send_target(target)
    assert actual.availability_reason == state
    assert classify_target_admission(actual) == "fail"


def test_fits_now_is_runnable_for_a_person_send(isolated_db, monkeypatch):
    from infra.send_target import classify_target_admission, person_send_target
    from llm.local_model_load import Readiness

    target = _not_loaded_target()
    monkeypatch.setattr("llm.local_model_load.readiness", lambda *_a, **_k: Readiness("fits_now"))
    assert classify_target_admission(person_send_target(target)) == "run"


def test_out_of_scope_readiness_keeps_the_cached_verdict(isolated_db, monkeypatch):
    from infra.send_target import person_send_target

    target = _not_loaded_target()
    monkeypatch.setattr("llm.local_model_load.readiness", lambda *_a, **_k: None)
    assert person_send_target(target) == target


@pytest.mark.asyncio
async def test_resolvers_keep_the_loaded_only_verdict(isolated_db, monkeypatch):
    """Only the person-send seam widens admission; every resolver waits for a not-loaded model."""
    from infra import send_target
    from llm.local_model_load import Readiness
    from tests.unavailable_model_support import mark_model_loaded, seed_model

    seed_model("omlx@srv/m")
    mark_model_loaded(monkeypatch, "omlx@srv/m", loaded=False)
    monkeypatch.setattr("llm.local_model_load.readiness", lambda *_a, **_k: Readiness("fits_now"))

    resolved = await send_target.resolve_target("omlx@srv/m")
    assert (resolved.available, resolved.availability_reason) == (False, "model_not_loaded")
    assert send_target.classify_target_admission(resolved) == "wait"
    assert send_target.model_admission("omlx@srv/m", person_send=False) == "wait"
    assert send_target.model_admission("omlx@srv/m", person_send=True) == "run"
