"""Setup status says whether a local setup flight was ever begun, so a resumed setup starts only a download that never was."""

from __future__ import annotations

import os
import sys
from dataclasses import replace

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from api import setup as setup_api  # noqa: E402
from llm import local_model, local_setup_operation  # noqa: E402
from llm.local_model import LocalModel  # noqa: E402

MODEL = LocalModel("Jundot/Qwen3.8-27B-oQ4e-mtp", 16_971_681_558, 262_144)


@pytest.fixture(autouse=True)
def setup_facts(isolated_db, monkeypatch, tmp_path):
    monkeypatch.setattr(setup_api.config, "STATE_ROOT", tmp_path)
    monkeypatch.setattr(setup_api.config, "WORKSPACE", tmp_path / "workspace")
    monkeypatch.setattr(setup_api.platform, "system", lambda: "Darwin")
    monkeypatch.setattr(setup_api.platform, "machine", lambda: "arm64")
    from infra.database import get_db

    db = get_db()  # a Mac that has never run setup lists no local model; the shared test database seeds one
    db.execute("DELETE FROM models WHERE provider = 'omlx'")
    db.commit()
    db.close()


def _flight(**changes) -> None:
    operation = local_setup_operation.Operation.for_model(MODEL, "setup", onboarding=True)
    local_setup_operation.write(replace(operation, **changes))


def _started() -> bool:
    return setup_api._setup_status()["local_started"]


def _omlx_model_row(available: int = 1) -> None:
    """A model on a local server that setup never downloaded (a taken-over install, a person's own oMLX)."""
    from infra.database import get_db

    db = get_db()
    db.execute(
        """INSERT INTO models
           (id, provider, provider_model_id, litellm_model, display_name, backend,
            source, enabled, available, created_at, updated_at)
           VALUES ('own-model', 'omlx', 'own-model', 'omlx@own/own-model', 'own-model', 'network',
                   'discovered', 1, ?, 'now', 'now')""",
        (available,),
    )
    db.commit()
    db.close()


def test_a_mac_that_never_began_a_local_flight_reports_not_started():
    assert _started() is False


@pytest.mark.parametrize("flight", [
    pytest.param({"task_id": "task-1"}, id="download-in-flight"),
    pytest.param({"result": {"ok": True, "model": MODEL.model}, "downloaded": True}, id="ready-or-removed-afterwards"),
    pytest.param({"result": {"ok": True, "cancelled": True}}, id="cancelled-by-the-person"),
    pytest.param({"result": {"ok": False, "cause": "other"}}, id="failed"),
    pytest.param({"result": {"ok": True, "downloading": True}}, id="still-downloading-in-omlx"),
])
def test_any_flight_that_was_begun_reports_started(flight):
    _flight(**flight)
    assert _started() is True


def test_a_model_a_finished_setup_recorded_reports_started_without_a_flight_record():
    local_model.record_set_up(MODEL)
    assert _started() is True


@pytest.mark.parametrize("available", [1, 0], ids=["listed", "server-offline"])
def test_a_model_already_on_a_local_server_reports_started_without_any_setup_record(available):
    _omlx_model_row(available)
    assert _started() is True


def test_a_record_the_app_cannot_read_does_not_count_as_a_started_flight():
    local_setup_operation.record_path().write_text("not json", encoding="utf-8")
    assert _started() is False
