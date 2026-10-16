"""Setup with the person's own oMLX: Use mine reads and downloads nothing; managing reads the key and picks."""

from __future__ import annotations

import asyncio

import pytest

from llm import local_setup, local_setup_existing, local_setup_ownership, own_omlx_management
from llm.local_model import LocalModel
from tests.test_local_setup_existing_omlx import _finished, _models, calls, fresh_flight  # noqa: F401

PICK = LocalModel("Jundot/Qwen3.8-27B-oQ4e-mtp", 16_971_681_558, 65_536)
THEIR_KEY = "their-admin-key"
THEIRS = [{"litellm_model": "omlx@local-server/my-model"}]


@pytest.fixture
def own(monkeypatch, tmp_path, calls):  # noqa: F811
    monkeypatch.setattr(own_omlx_management, "record_path", lambda: tmp_path / "own-omlx-management.json")
    monkeypatch.setattr(local_setup_ownership, "consent_marker_path", lambda: tmp_path / "consent")
    monkeypatch.setattr(local_setup, "existing_install_is_foreign", lambda: True)
    monkeypatch.setattr(local_setup_existing, "own_omlx_port", lambda: 8000)

    def read_key():
        calls.append("read_key")
        return THEIR_KEY

    async def register(*, port=8000, api_key=None):
        calls.append(("register", api_key))
        return "local-server"

    async def recommend(server_id):
        calls.append("recommend")
        return PICK

    async def download(server_id, report, model):
        calls.append(("download", model.model))

    async def rearm():
        calls.append("rearm")
        return False

    monkeypatch.setattr(local_setup_existing, "own_omlx_admin_key", read_key)
    monkeypatch.setattr(local_setup, "_register_loopback_server", register)
    monkeypatch.setattr("llm.local_model.ask_recommender", recommend)
    monkeypatch.setattr(local_setup, "_download", download)
    monkeypatch.setattr(local_setup, "_record_model", lambda model: calls.append("record_model"))
    monkeypatch.setattr("onboarding.omlx_settings_wake.rearm", rearm)
    _models(monkeypatch, THEIRS)
    return calls


def test_use_mine_never_reads_the_key_picks_or_downloads(own):
    local_setup_ownership.record_existing_omlx_consent(manage=False)

    result = asyncio.run(_finished())

    assert result["ok"] is True and result["models"] == 1
    assert own == ["health", ("register", None), "discover"]
    assert own_omlx_management.read() is None


def test_managing_reads_the_key_and_downloads_the_pick_when_it_is_absent(own):
    local_setup_ownership.record_existing_omlx_consent(manage=True)

    result = asyncio.run(_finished())

    assert result["ok"] is True
    assert own == ["health", "read_key", ("register", THEIR_KEY), "discover", "recommend", "record_model",
                   ("download", PICK.model), "discover", "rearm"]
    record = own_omlx_management.read()
    assert record.active and record.model == PICK and record.key_registered and record.cannot_check is None


def test_a_pick_the_person_already_has_is_not_downloaded_or_taken_over(own, monkeypatch):
    _models(monkeypatch, [*THEIRS, {"litellm_model": f"omlx@local-server/{PICK.model}"}])
    local_setup_ownership.record_existing_omlx_consent(manage=True)

    asyncio.run(_finished())

    assert ("download", PICK.model) not in own and "record_model" not in own
    assert own_omlx_management.read().model is None


def test_an_oMLX_too_old_to_recommend_is_recorded_and_nothing_is_picked(own, monkeypatch):
    monkeypatch.setattr(local_setup, "omlx_version", lambda: "0.7.0")
    local_setup_ownership.record_existing_omlx_consent(manage=True)

    asyncio.run(_finished())

    assert "recommend" not in own and not any(isinstance(call, tuple) and call[0] == "download" for call in own)
    assert own_omlx_management.read().cannot_check == "0.7.0"


def test_a_key_already_stored_is_kept_and_not_forgotten_on_stop(own, monkeypatch):
    async def stored(host, port):
        return "stored"

    monkeypatch.setattr(local_setup, "registered_local_key", stored)
    local_setup_ownership.record_existing_omlx_consent(manage=True)

    asyncio.run(_finished())

    assert ("register", None) in own
    assert own_omlx_management.read().key_registered is False
