"""A personal oMLX binary is never changed, and managed paths leave no install marker."""

import pytest

from llm import managed_omlx_binary as managed
from llm import local_setup_executable as installer


@pytest.fixture
def identity(monkeypatch, tmp_path):
    monkeypatch.setattr("infra.config.STATE_ROOT", tmp_path)
    return tmp_path


@pytest.mark.parametrize("inside,working", [(True, True), (False, True)])
def test_unowned_old_or_broken_binary_never_runs_homebrew(identity, monkeypatch, inside, working):
    binary = identity / ("keg/omlx" if inside else "personal/omlx")
    binary.parent.mkdir()
    binary.touch()
    monkeypatch.setattr(installer, "_is_broken_leftover", lambda _path: not working)
    monkeypatch.setattr(installer, "omlx_on_path", lambda: str(binary))
    monkeypatch.setattr(installer.shutil, "which", lambda _name: "/brew")
    monkeypatch.setattr(managed, "command_output", lambda cmd: str(identity / "keg") if "--prefix" in cmd else "omlx 0.7")
    monkeypatch.setattr(installer, "run_command", lambda _cmd: pytest.fail("personal binary must not be changed"))
    with pytest.raises(installer.OwnOmlxUpgradeRequired):
        installer.ensure_omlx(lambda _s: None, needs_repair=True)


@pytest.mark.asyncio
async def test_prepare_projects_personal_binary_update_cause_and_reference(monkeypatch):
    from llm import local_setup, local_setup_flow, local_setup_prepare as prepare

    monkeypatch.setattr(local_setup, "_flight", local_setup._Flight())
    monkeypatch.setattr(prepare, "_prepare", prepare._Prepare(state=prepare.CHECKING))
    monkeypatch.setattr(prepare, "local_setup_memory_ok", lambda: True)

    async def unowned(_report):
        raise installer.OwnOmlxUpgradeRequired("Update your oMLX, then Retry.")

    monkeypatch.setattr(prepare, "start_managed_omlx", unowned)
    await prepare._run()
    view = await prepare.pick_view()
    assert view["state"] == "failed"
    assert view["cause"] == "own_omlx_upgrade_required"
    assert view["ref"].startswith("local-")
    assert view["message"] == "Update your oMLX, then Retry."


def test_managed_paths_do_not_create_an_install_marker(tmp_path, monkeypatch):
    monkeypatch.setattr("infra.config.STATE_ROOT", tmp_path)
    assert managed.managed_omlx_paths().base_path == tmp_path / "managed-omlx"
    assert not (tmp_path / "managed-omlx-installed").exists()
