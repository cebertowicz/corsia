"""Send admission uses the local model readiness contract."""

from dataclasses import replace

import pytest


@pytest.mark.parametrize("state", ["no_room", "no_memory", "unreachable"])
def test_unready_local_target_fails_admission(isolated_db, monkeypatch, state):
    from infra.send_target import _admitted_target, classify_target_admission
    from llm.local_model_load import Readiness
    from llm.resolution import resolve_execution_target
    from tests.unavailable_model_support import seed_model

    seed_model("omlx@srv/m")
    target = resolve_execution_target("omlx@srv/m")
    monkeypatch.setattr("llm.local_model_load.readiness", lambda *_a, **_k: Readiness(state, 1))
    actual = _admitted_target(replace(target, available=True))
    assert actual.availability_reason == state
    assert classify_target_admission(actual) == "fail"


def test_fits_now_is_runnable(isolated_db, monkeypatch):
    from infra.send_target import _admitted_target, classify_target_admission
    from llm.local_model_load import Readiness
    from llm.resolution import resolve_execution_target
    from tests.unavailable_model_support import seed_model

    seed_model("omlx@srv/m")
    target = resolve_execution_target("omlx@srv/m")
    monkeypatch.setattr("llm.local_model_load.readiness", lambda *_a, **_k: Readiness("fits_now"))
    assert classify_target_admission(_admitted_target(target)) == "run"


@pytest.mark.parametrize("person_send", [True, False])
def test_out_of_scope_readiness_keeps_the_cached_verdict(isolated_db, monkeypatch, person_send):
    from infra.send_target import _admitted_target, _ready_target
    from llm.resolution import resolve_execution_target
    from tests.unavailable_model_support import seed_model

    seed_model("omlx@srv/m")
    target = resolve_execution_target("omlx@srv/m")
    monkeypatch.setattr("llm.local_model_load.readiness", lambda *_a, **_k: None)
    assert _admitted_target(target, person_send=person_send) == _ready_target(target)


@pytest.mark.parametrize(("refusal", "hold"), [
    ("no_room", "model_not_loaded"), ("no_memory", "model_not_loaded"), ("unreachable", "model_unavailable"),
])
def test_other_work_on_a_refused_target_waits_for_the_model(isolated_db, refusal, hold):
    from infra.send_target import _admitted_target, classify_target_admission
    from llm.resolution import resolve_execution_target
    from tests.unavailable_model_support import seed_model

    seed_model("omlx@srv/m")
    refused = replace(resolve_execution_target("omlx@srv/m"), available=False, availability_reason=refusal)
    actual = _admitted_target(refused, person_send=False)
    assert actual.availability_reason == hold
    assert classify_target_admission(actual) == "wait"
