"""A v0.3/v0.4 install's local models move to the managed oMLX without loss."""

from __future__ import annotations

import errno
import os
import shutil
from pathlib import Path

import pytest

from llm.local_model import LocalModel

MODEL = LocalModel("Jundot/Qwen3.8-27B-oQ4e", 3_000, 32_768)
PERSONS = "someone/their-own-model"


def _write_model(folder: Path, files: dict[str, int]) -> None:
    folder.mkdir(parents=True, exist_ok=True)
    for name, size in files.items():
        path = folder / name
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("wb") as handle:
            handle.truncate(size)


def _tree(folder: Path) -> dict[str, int]:
    return {
        str(path.relative_to(folder)): path.lstat().st_size
        for path in sorted(folder.rglob("*")) if path.is_file()
    }


@pytest.fixture
def legacy(monkeypatch, tmp_path):
    """A v0.4 Homebrew oMLX root under a scratch HOME, and a scratch CORSIA state root."""
    home = tmp_path / "home"
    state = tmp_path / "state"
    state.mkdir()
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setattr("infra.config.STATE_ROOT", state)
    root = home / ".omlx"
    models = root / "models"
    _write_model(models / MODEL.repository, {"model.safetensors": 2_000, "config.json": 1_000})
    _write_model(models / PERSONS, {"weights.safetensors": 5_000})
    (root / "settings.json").write_text('{"server": {"port": 8000}}', encoding="utf-8")
    return root


def _managed_dir() -> Path:
    from llm.local_setup_managed import managed_omlx_paths

    return managed_omlx_paths().model_dir


def test_legacy_model_dir_reads_the_old_settings(legacy, tmp_path) -> None:
    from llm.local_setup_upgrade import legacy_model_dir

    assert legacy_model_dir() == legacy / "models"
    custom = tmp_path / "elsewhere"
    (legacy / "settings.json").write_text(f'{{"model": {{"model_dir": "{custom}"}}}}', encoding="utf-8")
    assert legacy_model_dir() == custom


def test_only_models_corsia_recorded_are_listed(legacy, monkeypatch, tmp_path) -> None:
    from llm import local_downloads
    from llm.local_model import record_set_up
    from llm.local_setup_upgrade import recorded_models

    monkeypatch.setattr(local_downloads, "_path", lambda: tmp_path / "local-downloads.json")
    extra = LocalModel("Jundot/Gemma-4-12B-oQ4e", 10, 32_768)
    failed = LocalModel("Jundot/Other-oQ4e", 10, 32_768)
    record_set_up(MODEL)
    local_downloads.write(local_downloads.Download(extra, status="available"))
    local_downloads.write(local_downloads.Download(failed, status="failed"))

    assert [model.repository for model in recorded_models(legacy / "models")] == [
        MODEL.repository, extra.repository,
    ]


def test_a_pre_record_starter_counts_only_when_complete(legacy, monkeypatch) -> None:
    from llm import local_profiles
    from llm.local_setup_upgrade import recorded_models

    starter = LocalModel(MODEL.repository, 3_000, 32_768)
    monkeypatch.setattr(local_profiles, "STARTER_MODEL", starter)
    assert recorded_models(legacy / "models") == [starter]

    monkeypatch.setattr(local_profiles, "STARTER_MODEL", LocalModel(MODEL.repository, 9_999, 32_768))
    assert recorded_models(legacy / "models") == []


def test_same_volume_move_takes_only_the_recorded_model(legacy) -> None:
    from llm.local_setup_upgrade import move_recorded_models

    before = _tree(legacy / "models" / MODEL.repository)
    persons = _tree(legacy / "models" / PERSONS)

    moved = move_recorded_models(legacy / "models", [MODEL])

    assert [item.outcome for item in moved] == ["moved"]
    assert _tree(_managed_dir() / MODEL.repository) == before
    assert not (legacy / "models" / MODEL.repository).exists()
    assert _tree(legacy / "models" / PERSONS) == persons
    assert (legacy / "settings.json").read_text(encoding="utf-8") == '{"server": {"port": 8000}}'


def test_rerun_after_success_changes_nothing(legacy) -> None:
    from llm.local_setup_upgrade import move_recorded_models

    move_recorded_models(legacy / "models", [MODEL])
    after = _tree(_managed_dir() / MODEL.repository)

    assert [item.outcome for item in move_recorded_models(legacy / "models", [MODEL])] == ["already"]
    assert _tree(_managed_dir() / MODEL.repository) == after


def _cross_volume(monkeypatch) -> None:
    """Make the source-to-target rename fail as it does across volumes."""
    real_rename = os.rename

    def rename(source, target):
        if ".moving" not in str(source) and "managed-omlx" in str(target):
            raise OSError(errno.EXDEV, "Cross-device link")
        return real_rename(source, target)

    monkeypatch.setattr("llm.local_setup_upgrade.os.rename", rename)


def test_cross_volume_copy_is_verified_before_the_original_goes(legacy, monkeypatch) -> None:
    from llm.local_setup_upgrade import move_recorded_models

    _cross_volume(monkeypatch)
    before = _tree(legacy / "models" / MODEL.repository)

    assert [item.outcome for item in move_recorded_models(legacy / "models", [MODEL])] == ["moved"]
    assert _tree(_managed_dir() / MODEL.repository) == before
    assert not (legacy / "models" / MODEL.repository).exists()
    assert not any(path.name.endswith(".moving") for path in _managed_dir().rglob("*"))


def test_a_copy_failing_midway_leaves_the_original_and_its_record(legacy, monkeypatch) -> None:
    from llm.local_model import record_set_up, set_up_model
    from llm.local_setup_upgrade import move_recorded_models

    record_set_up(MODEL)
    _cross_volume(monkeypatch)
    before = _tree(legacy / "models" / MODEL.repository)

    def copy_half(source, target, **_kwargs):
        Path(target).mkdir(parents=True)
        shutil.copy2(Path(source) / "config.json", Path(target) / "config.json")
        raise OSError(errno.ENOSPC, "No space left on device")

    monkeypatch.setattr("llm.local_setup_upgrade.shutil.copytree", copy_half)

    assert [item.outcome for item in move_recorded_models(legacy / "models", [MODEL])] == ["failed"]
    assert _tree(legacy / "models" / MODEL.repository) == before
    assert not (_managed_dir() / MODEL.repository).exists()
    assert not any(path.name.endswith(".moving") for path in _managed_dir().rglob("*"))
    assert set_up_model() == MODEL


def test_a_copy_that_does_not_match_is_discarded(legacy, monkeypatch) -> None:
    from llm.local_setup_upgrade import move_recorded_models

    _cross_volume(monkeypatch)
    before = _tree(legacy / "models" / MODEL.repository)

    def copy_short(source, target, **_kwargs):
        Path(target).mkdir(parents=True)
        shutil.copy2(Path(source) / "config.json", Path(target) / "config.json")

    monkeypatch.setattr("llm.local_setup_upgrade.shutil.copytree", copy_short)

    assert [item.outcome for item in move_recorded_models(legacy / "models", [MODEL])] == ["failed"]
    assert _tree(legacy / "models" / MODEL.repository) == before
    assert not (_managed_dir() / MODEL.repository).exists()


def test_an_interrupted_move_finishes_on_the_next_start(legacy) -> None:
    """A leftover staging copy is redone; a verified copy whose original survived is completed."""
    from llm.local_setup_upgrade import move_recorded_models

    source = legacy / "models" / MODEL.repository
    before = _tree(source)
    staging = _managed_dir() / "Jundot" / f".{Path(MODEL.repository).name}.moving"
    _write_model(staging, {"model.safetensors": 7})

    assert [item.outcome for item in move_recorded_models(legacy / "models", [MODEL])] == ["moved"]
    assert _tree(_managed_dir() / MODEL.repository) == before

    # The original came back identical (a removal that failed): the next start removes it.
    shutil.copytree(_managed_dir() / MODEL.repository, source)
    assert [item.outcome for item in move_recorded_models(legacy / "models", [MODEL])] == ["moved"]
    assert not source.exists()


def test_a_differing_managed_copy_keeps_both(legacy) -> None:
    from llm.local_setup_upgrade import move_recorded_models

    source = legacy / "models" / MODEL.repository
    before = _tree(source)
    _write_model(_managed_dir() / MODEL.repository, {"model.safetensors": 1})

    assert [item.outcome for item in move_recorded_models(legacy / "models", [MODEL])] == ["already"]
    assert _tree(source) == before


# The whole upgrade, as startup runs it.

@pytest.fixture
def upgrade_env(legacy, isolated_db, monkeypatch):
    """A v0.4.0 install: CORSIA's Homebrew oMLX runs as a launchd job and holds the starter."""
    from llm import local_profiles
    from llm import local_setup as ls
    from llm import local_setup_new_omlx, local_setup_ownership as ownership
    from llm.local_setup_ownership import marker_path

    marker_path().write_text("installed by corsia local setup\n", encoding="utf-8")
    starter = LocalModel(MODEL.repository, 3_000, 32_768)
    monkeypatch.setattr(local_profiles, "STARTER_MODEL", starter)
    monkeypatch.setattr(ownership, "legacy_setup_owns_current_omlx", lambda: True)
    service = {"running": True}
    events: list[str] = []

    def retire() -> bool:
        events.append("stop service")
        service["running"] = False
        return True

    monkeypatch.setattr(ownership, "retire_legacy_owned_omlx_service", retire)
    monkeypatch.setattr(ownership, "launchd_jobs",
                        lambda: {101: ownership.BREW_SERVICE_LABEL} if service["running"] else {})
    monkeypatch.setattr(ownership, "_omlx_serve_processes",
                        lambda: [(101, 1)] if service["running"] else [])

    async def start_managed(report):
        from llm.local_setup_managed import managed_omlx_paths
        from llm.local_setup_runtime import register_managed_server
        from llm.network_discovery import update_server_status

        assert not service["running"], "the old service must stop before the move"
        assert (managed_omlx_paths().model_dir / MODEL.repository).is_dir()
        paths = managed_omlx_paths()
        server_id, _key = await register_managed_server(
            port=8210, base_path=str(paths.base_path), model_dir=str(paths.model_dir))
        update_server_status(server_id, status="online")
        events.append("start managed")
        return server_id, 8210

    async def discover(server_id):
        from infra.database import get_db

        db = get_db()
        db.execute(
            """INSERT INTO models (id, provider, provider_model_id, litellm_model, display_name,
                   backend, source, enabled, available, created_at, updated_at)
               VALUES (?, 'omlx', ?, ?, 'Qwen', 'network', 'discovered', 1, 1, 'now', 'now')""",
            ("managed-qwen", MODEL.model, f"omlx@{server_id}/{MODEL.model}"),
        )
        db.commit()
        db.close()
        events.append("discover")
        return {"ok": True}

    async def configure(_server_id, model):
        events.append(f"configure {model.repository}")

    async def no_download(*_args, **_kwargs):
        pytest.fail("an upgrade must not download")

    monkeypatch.setattr(local_setup_new_omlx, "start_managed_omlx", start_managed)
    monkeypatch.setattr("llm.catalog_network_models.discover_network_models", discover)
    monkeypatch.setattr(ls, "_configure_model_settings", configure)
    monkeypatch.setattr(ls, "_download", no_download)
    monkeypatch.setattr(ls, "local_memory_ok", lambda: True)
    return events


@pytest.mark.asyncio
async def test_v040_install_keeps_its_starter_ready_with_no_question_and_no_download(
        upgrade_env, legacy, isolated_db) -> None:
    from infra.db_async import run_in_db
    from llm import network_discovery
    from llm.local_model import set_up_model
    from llm.local_setup_ownership import apply_foreign_omlx_choice
    from llm.local_setup_upgrade import done_path, upgrade_legacy_omlx
    from llm.local_sources import _omlx_state

    old = await run_in_db(network_discovery.upsert_server,
        provider="omlx", instance_id="127.0.0.1:8000", instance_name="This Mac",
        host="127.0.0.1", port=8000, status="online", kind="managed")
    # CORSIA's own service is not the person's oMLX: setup asks nothing.
    assert apply_foreign_omlx_choice(consent_existing=False, manage_existing=None) is None

    moved = await upgrade_legacy_omlx()

    assert [(item.repository, item.outcome) for item in moved] == [(MODEL.repository, "moved")]
    assert upgrade_env == ["stop service", "start managed", "discover", f"configure {MODEL.repository}"]
    assert set_up_model().repository == MODEL.repository
    assert (await run_in_db(network_discovery.get_server, old))["status"] == "removed"
    assert (await run_in_db(_omlx_state))["state"] == "ready"
    assert (legacy / "models" / PERSONS).is_dir()
    assert done_path().exists()
    # A second start does nothing at all.
    assert await upgrade_legacy_omlx() is None
    assert upgrade_env == ["stop service", "start managed", "discover", f"configure {MODEL.repository}"]


@pytest.mark.asyncio
async def test_a_failed_move_leaves_the_install_as_it_was_and_retries(upgrade_env, legacy, monkeypatch) -> None:
    from llm.local_setup_upgrade import done_path, upgrade_legacy_omlx

    before = _tree(legacy / "models" / MODEL.repository)
    _cross_volume(monkeypatch)

    def copy_fails(*_args, **_kwargs):
        raise OSError(errno.ENOSPC, "No space left on device")

    monkeypatch.setattr("llm.local_setup_upgrade.shutil.copytree", copy_fails)

    moved = await upgrade_legacy_omlx()

    assert [item.outcome for item in moved] == ["failed"]
    assert _tree(legacy / "models" / MODEL.repository) == before
    assert "start managed" not in upgrade_env
    assert not done_path().exists()


@pytest.mark.asyncio
async def test_a_service_that_will_not_stop_moves_nothing(upgrade_env, legacy, monkeypatch) -> None:
    from llm import local_setup_ownership as ownership
    from llm.local_setup_upgrade import upgrade_legacy_omlx

    monkeypatch.setattr(ownership, "retire_legacy_owned_omlx_service", lambda: False)

    assert await upgrade_legacy_omlx() is None
    assert (legacy / "models" / MODEL.repository).is_dir()
    assert not (_managed_dir() / MODEL.repository).exists()


@pytest.mark.asyncio
async def test_an_unproven_install_is_left_alone(legacy, monkeypatch) -> None:
    from llm import local_setup_ownership as ownership
    from llm.local_setup_upgrade import upgrade_legacy_omlx

    monkeypatch.setattr(ownership, "legacy_setup_owns_current_omlx", lambda: False)
    monkeypatch.setattr(ownership, "retire_legacy_owned_omlx_service", lambda: pytest.fail("must not stop"))

    assert await upgrade_legacy_omlx() is None
    assert (legacy / "models" / MODEL.repository).is_dir()


@pytest.mark.asyncio
async def test_upgrade_waits_for_no_running_setup_flight(upgrade_env, legacy) -> None:
    from llm import local_setup_operation
    from llm.local_setup_upgrade import upgrade_legacy_omlx

    held = local_setup_operation.try_lock()
    try:
        assert await upgrade_legacy_omlx() is None
    finally:
        local_setup_operation.unlock(held)
    assert upgrade_env == []
    assert (legacy / "models" / MODEL.repository).is_dir()
