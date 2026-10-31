"""Work already under way stops at its next call once locality policy forbids its model.

The native per-call refresh and Claude Code steering resolve through the thread's resolver,
so a model the project turned off is never called again; later input then holds at admission.
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from infra.database import get_db
from tests.unavailable_model_support import seed_model

pytestmark = pytest.mark.usefixtures("signed_in_defaults")  # a harness turn needs a signed-in Default: no ambient login

CLOUD = "anthropic/x"
OFF = "project-off"


def _cloud_off(db, off: bool = True) -> None:
    from llm.locality import set_project_cloud_models_off

    set_project_cloud_models_off(OFF, cloud_models_off=off, db=db)


@pytest.fixture
def project(isolated_db, monkeypatch):
    monkeypatch.setattr("llm.model_status._cloud_providers_active", lambda: {"anthropic"})
    db = get_db()
    try:
        db.execute(
            "INSERT INTO projects (id,name,path,created_at,updated_at) VALUES (?,?,'/tmp','t','t')", (OFF, OFF),
        )
        db.commit()
        _cloud_off(db)
    finally:
        db.close()
    return isolated_db


@pytest.mark.asyncio
async def test_native_refresh_stops_a_call_under_way_once_policy_forbids_its_model(project):
    from core.turn_iteration_context import _refresh_native_window
    from llm.resolution import ModelUnavailableError
    from prompts.cloud_off import held_error

    seed_model(CLOUD, backend="api", provider="anthropic")
    db = get_db()
    try:
        db.execute(
            "INSERT INTO threads (id,user_id,model,project_id,created_at,updated_at)"
            " VALUES ('t-off','user-1',?,?,'t0','t0')",
            (CLOUD, OFF),
        )
        db.commit()
    finally:
        db.close()
    state = SimpleNamespace(fit_ledger=SimpleNamespace(target_catalog_id=CLOUD, effective_window_tokens=32768))

    with pytest.raises(ModelUnavailableError) as refused:
        await _refresh_native_window(state, "t-off")

    assert refused.value.reason == held_error(CLOUD)
    db = get_db()
    try:
        _cloud_off(db, False)
    finally:
        db.close()
    await _refresh_native_window(state, "t-off")


@pytest.mark.asyncio
async def test_cc_steering_declines_and_leaves_the_head_queued_once_cloud_is_off(project, monkeypatch):
    import subagents.cc_runtime_steering as steering
    from tests.test_clean_cc_boundary import _install_runtime, _seed_boundary, _steer_row

    db = get_db()
    try:
        current = _seed_boundary(db)
        db.execute("UPDATE threads SET project_id=? WHERE id='thread-1'", (OFF,))
        db.commit()
    finally:
        db.close()

    async def feed(*_args, **_kwargs):
        raise AssertionError("a cloud-off steer reached Claude Code")

    _install_runtime(monkeypatch, steering, current, feed)

    assert await steering.deliver_live_inbox_head("thread-1") is False
    db = get_db()
    try:
        assert tuple(_steer_row(db))[:3] == (None, "queued", None)
    finally:
        db.close()
