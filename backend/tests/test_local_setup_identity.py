"""The same ownership rules protect process choices, binaries and status reads."""

from types import SimpleNamespace

import psutil
import pytest

from llm import local_setup_foreign as foreign, managed_omlx_binary as managed
from llm import local_setup_ownership as installer


def _process(pid, argv, *, name="Python", uid=501, status=psutil.STATUS_RUNNING):
    return SimpleNamespace(info={
        "pid": pid, "ppid": 1, "name": name, "cmdline": argv,
        "uids": SimpleNamespace(real=uid), "status": status,
    })


@pytest.fixture
def identity(monkeypatch, tmp_path):
    monkeypatch.setattr("infra.config.STATE_ROOT", tmp_path)

    monkeypatch.setattr(managed, "launchd_jobs", lambda: {})
    monkeypatch.setattr(managed.os, "getuid", lambda: 501)
    return tmp_path


def test_only_supervisor_records_prove_identity_even_after_title_rewrite(identity, monkeypatch, isolated_db):
    from llm.providers import omlx_supervisor as supervisor

    monkeypatch.setattr(supervisor.process.psutil, "Process", lambda _pid: SimpleNamespace(
        create_time=lambda: 123.5, is_running=lambda: True, status=lambda: psutil.STATUS_RUNNING,
    ))
    supervisor._write_pid("owned-a", 101)
    supervisor._write_pid("owned-rewritten", 107)
    base = managed.managed_omlx_paths().base_path
    alias = identity / "alias with spaces"
    alias.symlink_to(base, target_is_directory=True)
    processes = [
        _process(101, ["python", "/bin/omlx", "serve", "--base-path", str(base)]),
        _process(102, ["omlx", "serve", f"--base-path={alias}"]),
        _process(103, ["omlx", "serve", "--base-path", str(base) + "-personal"]),
        _process(104, ["omlx", "serve", "--model-dir", str(base)]),
        _process(105, ["omlx", "serve", "--base-path", str(base), "--base-path", "/personal"]),
        _process(106, ["echo", "omlx", "serve", "--base-path", str(base)]),
        _process(107, ["omlx-server", "", "", ""], name="python3.11"),
    ]
    monkeypatch.setattr(managed.psutil, "process_iter", lambda _attrs: processes)
    result = managed.omlx_processes()
    assert result is not None
    assert [(p.pid, p.owner) for p in result] == [
        (101, "managed"), (102, "personal"), (103, "personal"), (104, "personal"), (105, "personal"),
        (107, "managed"),
    ]
    monkeypatch.setattr(managed.psutil, "process_iter", lambda _attrs: [processes[-1]])
    monkeypatch.setattr(foreign.os, "kill", lambda *_args: pytest.fail("must never stop the managed process"))
    assert foreign.apply_foreign_omlx_choice() is None
    assert foreign.apply_foreign_omlx_choice(other_omlx="stop") is None
    assert not foreign.foreign_omlx_choice_path().exists()


def test_no_process_is_distinct_from_unknown(identity, monkeypatch):
    monkeypatch.setattr(managed.psutil, "process_iter", lambda _attrs: [])
    assert managed.foreign_omlx_processes() == ()

    def unreadable(_attrs):
        raise psutil.AccessDenied()

    monkeypatch.setattr(managed.psutil, "process_iter", unreadable)
    assert managed.foreign_omlx_processes() is None


@pytest.mark.parametrize("name", ["python3.11", "omlx", "omlx-server"])
@pytest.mark.parametrize("argv", [None, []])
def test_same_user_unreadable_omlx_candidate_continues_without_stopping(identity, monkeypatch, caplog, name, argv):
    monkeypatch.setattr(managed.psutil, "process_iter", lambda _attrs: [_process(101, argv, name=name)])
    monkeypatch.setattr(foreign, "stop_foreign_omlx_processes", lambda _p: pytest.fail("must not stop"))

    assert foreign.apply_foreign_omlx_choice(other_omlx="stop") is None
    assert "continuing without stopping reference=local-" in caplog.text
    assert not foreign.foreign_omlx_choice_path().exists()


def test_zombie_and_other_user_processes_are_skipped(identity, monkeypatch):
    processes = [
        _process(101, None, status=psutil.STATUS_ZOMBIE),
        _process(102, None, uid=502),
    ]
    monkeypatch.setattr(managed.psutil, "process_iter", lambda _attrs: processes)

    assert managed.foreign_omlx_processes() == ()


def test_enumeration_failure_remains_unknown(identity, monkeypatch):
    def failing_iterator(_attrs):
        raise psutil.AccessDenied()

    monkeypatch.setattr(managed.psutil, "process_iter", failing_iterator)
    assert managed.foreign_omlx_processes() is None


def test_one_offer_covers_every_personal_process_and_stop_uses_that_set(identity, monkeypatch):
    processes = (managed.OmlxProcess(101, 1, None, "personal"),
                 managed.OmlxProcess(102, 1, None, "personal"))
    monkeypatch.setattr(foreign, "foreign_omlx_processes", lambda: processes)
    killed = []
    monkeypatch.setattr(foreign.os, "kill", lambda pid, sig: killed.append((pid, sig)))
    assert foreign.apply_foreign_omlx_choice() == "consent_required"
    assert foreign.apply_foreign_omlx_choice(other_omlx="stop") is None
    assert killed == [(101, 15), (102, 15)]
    assert foreign.foreign_omlx_answer() == "stopped_theirs"


def test_stop_never_transfers_to_a_changed_process_set(identity, monkeypatch):
    current = [managed.OmlxProcess(101, 1, None, "personal")]
    monkeypatch.setattr(foreign, "foreign_omlx_processes", lambda: tuple(current))
    monkeypatch.setattr(foreign.os, "kill", lambda *_a: pytest.fail("changed PIDs must not stop"))
    assert foreign.apply_foreign_omlx_choice() == "consent_required"
    current.append(managed.OmlxProcess(102, 1, None, "personal"))
    assert foreign.apply_foreign_omlx_choice(other_omlx="stop") == "foreign_omlx_changed"
    assert foreign.foreign_omlx_answer() == "none"


def test_unknown_revalidation_continues_without_stopping_or_recording_success(identity, monkeypatch, caplog):
    original = (managed.OmlxProcess(101, 1, None, "personal"),)
    reads = iter([original, original, None])
    monkeypatch.setattr(foreign, "foreign_omlx_processes", lambda: next(reads))
    monkeypatch.setattr(foreign.os, "kill", lambda *_a: pytest.fail("unknown set must not stop"))
    assert foreign.apply_foreign_omlx_choice() == "consent_required"
    assert foreign.apply_foreign_omlx_choice(other_omlx="stop") is None
    assert foreign.foreign_omlx_answer() == "none"
    assert "continuing without stopping reference=local-" in caplog.text


def test_keep_both_and_status_survive_a_pid_change_without_process_reads(identity, monkeypatch):
    current = [managed.OmlxProcess(101, 1, None, "personal")]
    monkeypatch.setattr(foreign, "foreign_omlx_processes", lambda: tuple(current))
    assert foreign.apply_foreign_omlx_choice(other_omlx="keep") is None
    current[:] = [managed.OmlxProcess(102, 1, None, "personal")]
    assert foreign.apply_foreign_omlx_choice() is None
    monkeypatch.setattr(foreign, "foreign_omlx_processes", lambda: pytest.fail("status must not inspect processes"))
    assert foreign.foreign_omlx_answer() == "kept_both"


@pytest.mark.parametrize("marked,inside,working", [
    (False, True, True), (True, False, True),
])
def test_unowned_old_or_broken_binary_never_runs_homebrew(identity, monkeypatch, marked, inside, working):
    binary = identity / ("keg/omlx" if inside else "personal/omlx")
    binary.parent.mkdir()
    binary.touch()
    if marked:
        managed.marker_path().touch()
    monkeypatch.setattr(installer, "_is_broken_leftover", lambda _path: not working)
    monkeypatch.setattr(installer, "omlx_on_path", lambda: str(binary))
    monkeypatch.setattr(installer.shutil, "which", lambda _name: "/brew")
    monkeypatch.setattr(managed, "command_output", lambda cmd: str(identity / "keg") if "--prefix" in cmd else "omlx 0.7")
    monkeypatch.setattr(installer, "run_command", lambda _cmd: pytest.fail("personal binary must not be changed"))
    with pytest.raises(installer.OwnOmlxUpgradeRequired):
        installer.ensure_omlx(lambda _s: None, needs_repair=True)


@pytest.mark.asyncio
async def test_prepare_projects_personal_binary_update_cause_and_reference(monkeypatch):
    from llm import local_setup, local_setup_new_omlx, local_setup_prepare as prepare

    monkeypatch.setattr(local_setup, "_flight", local_setup._Flight())
    monkeypatch.setattr(prepare, "_prepare", prepare._Prepare(state=prepare.CHECKING))
    monkeypatch.setattr(prepare, "local_memory_ok", lambda: True)

    async def unowned(_report):
        raise installer.OwnOmlxUpgradeRequired("Update your oMLX, then Retry.")

    monkeypatch.setattr(prepare, "start_managed_omlx", unowned)
    await prepare._run()
    view = await prepare.pick_view()
    assert view["state"] == "failed"
    assert view["cause"] == "own_omlx_upgrade_required"
    assert view["ref"].startswith("local-")
    assert view["message"] == "Update your oMLX, then Retry."


def test_stop_revalidation_keeps_unknown_distinct(identity, monkeypatch):
    process = managed.OmlxProcess(101, 1, None, "personal")
    monkeypatch.setattr(foreign, "foreign_omlx_processes", lambda: None)

    assert foreign.stop_foreign_omlx_processes((process,)) == "unknown"


def test_fresh_install_records_only_managed_ownership(tmp_path, monkeypatch):
    monkeypatch.setattr("infra.config.STATE_ROOT", tmp_path)
    legacy = tmp_path / "local_setup_installed_omlx"
    legacy.write_text("legacy proof")
    managed.mark_installed_by_setup()
    assert managed.marker_path().exists()
    assert legacy.read_text() == "legacy proof"
    assert not (tmp_path / "legacy-omlx-upgraded").exists()
