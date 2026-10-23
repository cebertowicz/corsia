"""The shell's unfinished-setup marker: the backend records what setup creates and removes it before finishing."""

from __future__ import annotations

import hashlib
import json
import os
import sys
from pathlib import Path

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import infra.setup_marker as setup_marker  # noqa: E402  # mypy resolves a new submodule only this way
from infra import accounts, config, setup_progress  # noqa: E402
from infra.database import get_db, write_transaction  # noqa: E402
from infra.user_profile import PROFILE_RELATIVE_PATH  # noqa: E402

PASSWORD = "correct horse battery staple 7"
PROFILE = PROFILE_RELATIVE_PATH.as_posix()
REPO = Path(__file__).resolve().parents[2]


@pytest.fixture
def app(isolated_db, tmp_path, monkeypatch) -> Path:
    """An app folder whose state root the shell marked: the layout `service.json` records on a first run."""
    app_dir = tmp_path / "app"
    state, workspace = app_dir / "state", tmp_path / "work"
    state.mkdir(parents=True)
    workspace.mkdir()
    monkeypatch.setattr(config, "STATE_ROOT", state)
    monkeypatch.setattr(config, "WORKSPACE", workspace)
    with write_transaction(None, scope="test_clear_accounts") as db:
        db.execute("DELETE FROM users")
    (app_dir / setup_marker.MARKER_FILE).write_text(
        json.dumps({"state_dir": str(state), "workspace": str(tmp_path / "default"), "files": {}}), encoding="utf-8",
    )
    return app_dir


def _marker(app_dir: Path) -> dict:
    return json.loads((app_dir / setup_marker.MARKER_FILE).read_text(encoding="utf-8"))


def _first_account(path: str = "app", email: str = "pat@example.test") -> str:
    return accounts.create_first_account(
        "pat", PASSWORD, full_name="Pat Example", email=email, first_run_path=path,
    )


def _completed() -> bool:
    return bool((setup_progress.read() or {}).get("setup_completed"))


def test_the_marker_name_is_the_one_the_shell_uses():
    shell = (REPO / "desktop" / "src-tauri" / "src" / "unfinished_setup.rs").read_text(encoding="utf-8")
    assert f'pub const MARKER_FILE: &str = "{setup_marker.MARKER_FILE}";' in shell


def test_the_app_setup_records_the_profile_it_created_with_its_digest(app):
    _first_account()
    profile = (config.WORKSPACE / PROFILE).read_bytes()
    marker = _marker(app)
    assert marker["files"] == {PROFILE: hashlib.sha256(profile).hexdigest()}
    assert marker["files_root"] == str(config.WORKSPACE)
    assert marker["workspace"] == str(app.parent / "default"), "the folder the shell's first run made is never rewritten"


def test_a_profile_the_person_already_had_is_not_recorded(app):
    (config.WORKSPACE / "context").mkdir()
    (config.WORKSPACE / PROFILE).write_text("# Mine\n- **Preferred name:** Pat\n", encoding="utf-8")
    _first_account()
    assert _marker(app)["files"] == {}


def test_a_script_made_first_account_leaves_no_marker_so_a_relaunch_keeps_everything(app):
    _first_account("script")
    assert not (app / setup_marker.MARKER_FILE).exists(), "setup is finished the moment the CLI makes the account"
    assert setup_progress.view()["setup_completed"]


def test_the_command_line_first_account_removes_the_marker_too(app):
    accounts.create_first_account("pat", PASSWORD)  # what `corsia user add` calls: no profile, the script path
    assert not (app / setup_marker.MARKER_FILE).exists()


def test_an_account_made_outside_the_app_is_refused_when_its_marker_will_not_go(app):
    app.chmod(0o500)
    try:
        with pytest.raises(OSError):
            accounts.create_first_account("pat", PASSWORD)
    finally:
        app.chmod(0o700)
    assert not get_db().execute("SELECT 1 FROM users").fetchone()
    assert (app / setup_marker.MARKER_FILE).exists(), "the install is still unfinished, and still marked"


def test_startup_removes_the_marker_of_an_install_that_already_reads_finished(app):
    _first_account("script")
    marker = {"state_dir": str(config.STATE_ROOT), "workspace": str(app.parent / "default"), "files": {}}
    (app / setup_marker.MARKER_FILE).write_text(json.dumps(marker), encoding="utf-8")
    setup_progress.release_marker_if_finished()
    assert not (app / setup_marker.MARKER_FILE).exists()


def test_startup_leaves_the_marker_while_setup_is_unfinished_or_has_no_account_yet(app):
    setup_progress.release_marker_if_finished()
    assert (app / setup_marker.MARKER_FILE).exists(), "no account yet: the first run is still being set up"
    _first_account("app")
    setup_progress.release_marker_if_finished()
    assert (app / setup_marker.MARKER_FILE).exists(), "an account at a step: setup is unfinished"
    assert not setup_progress.view()["setup_completed"]


def test_the_backend_startup_releases_a_finished_installs_marker():
    import inspect

    import app_lifecycle

    assert "release_marker_at_startup" in inspect.getsource(app_lifecycle._initialize_startup)


def test_a_marker_that_will_not_go_at_startup_is_logged_and_never_stops_the_backend(app):
    _first_account("script")
    marker = {"state_dir": str(config.STATE_ROOT), "workspace": str(app.parent / "default"), "files": {}}
    (app / setup_marker.MARKER_FILE).write_text(json.dumps(marker), encoding="utf-8")
    logged = []

    class Logger:
        def exception(self, message):
            logged.append(message)

    app.chmod(0o500)
    try:
        setup_progress.release_marker_at_startup(Logger())
    finally:
        app.chmod(0o700)
    assert len(logged) == 1
    assert (app / setup_marker.MARKER_FILE).exists()


def test_a_wiped_install_has_no_step_to_resume(app):
    """After a wipe the database is new: no account and no record, so setup opens at screen 1, not as a resume."""
    assert setup_progress.read() is None
    assert setup_progress.view()["step"] is None
    assert not get_db().execute("SELECT 1 FROM users").fetchone()


def test_the_shells_kept_downloads_are_the_backends_model_store_assets_and_logs(app):
    from core.corrections_asset_files import asset_root
    from llm.managed_omlx_binary import managed_omlx_paths

    kept = [
        str(managed_omlx_paths().model_dir.relative_to(config.STATE_ROOT)),
        asset_root().relative_to(config.STATE_ROOT).parts[0],
        config.LOGS_DIR.name,
    ]
    shell = (REPO / "desktop" / "src-tauri" / "src" / "unfinished_setup.rs").read_text(encoding="utf-8")
    declared = shell.split("pub const KEPT: [&str; 3] = [", 1)[1].split("];", 1)[0]
    assert [part.strip().strip('"') for part in declared.split(",")] == kept


def test_an_install_without_a_marker_is_left_alone(app):
    (app / setup_marker.MARKER_FILE).unlink()
    _first_account()
    assert (config.WORKSPACE / PROFILE).is_file()
    assert not (app / setup_marker.MARKER_FILE).exists()
    assert setup_progress.update(completed=True) is None
    assert _completed()


def test_another_state_roots_marker_is_never_touched(app):
    other = json.dumps({"state_dir": str(app.parent / "elsewhere" / "state"), "workspace": "/w", "files": {}})
    (app / setup_marker.MARKER_FILE).write_text(other, encoding="utf-8")
    _first_account()
    setup_progress.update(completed=True)
    assert (app / setup_marker.MARKER_FILE).read_text(encoding="utf-8") == other


def test_a_marker_that_cannot_be_written_refuses_the_account_and_leaves_no_profile(app):
    app.chmod(0o500)
    try:
        with pytest.raises(OSError):
            _first_account()
    finally:
        app.chmod(0o700)
    assert not get_db().execute("SELECT 1 FROM users").fetchone()
    assert not (config.WORKSPACE / PROFILE).exists()


def test_finishing_removes_the_marker_before_the_completion_is_committed(app, monkeypatch):
    _first_account()
    seen = {}
    real = setup_progress.set_setting

    def check(key, value, **kwargs):
        seen["marker_present"] = (app / setup_marker.MARKER_FILE).exists()
        return real(key, value, **kwargs)

    monkeypatch.setattr(setup_progress, "set_setting", check)
    setup_progress.update(completed=True)
    assert seen == {"marker_present": False}
    assert _completed()


def test_a_marker_that_will_not_go_refuses_the_finish_and_setup_stays_unfinished(app, monkeypatch):
    _first_account()

    def refuse(self, *args, **kwargs):
        raise PermissionError("read-only app folder")

    with monkeypatch.context() as patched:
        patched.setattr(Path, "unlink", refuse)
        with pytest.raises(setup_progress.SetupProgressError):
            setup_progress.update(completed=True)
    assert not _completed()
    assert (app / setup_marker.MARKER_FILE).exists()
    setup_progress.update(completed=True)
    assert _completed()
    assert not (app / setup_marker.MARKER_FILE).exists()


def test_an_unreadable_marker_never_blocks_the_install_from_finishing_setup(app, monkeypatch):
    _first_account()
    real = Path.read_bytes

    def refuse(self):
        if self.name == setup_marker.MARKER_FILE:
            raise PermissionError("busy")
        return real(self)

    with monkeypatch.context() as patched:
        patched.setattr(Path, "read_bytes", refuse)
        setup_marker.record_created_file("context/x.md", "0" * 64)  # records nothing, refuses nothing
        setup_progress.update(completed=True)
    assert _completed()
    assert not (app / setup_marker.MARKER_FILE).exists(), "a finished install keeps no marker it could not verify"


def test_a_damaged_marker_never_blocks_the_install_from_finishing_setup(app):
    _first_account()
    (app / setup_marker.MARKER_FILE).write_text("{not json", encoding="utf-8")
    setup_marker.record_created_file("context/x.md", "0" * 64)  # records nothing, refuses nothing
    assert (app / setup_marker.MARKER_FILE).read_text(encoding="utf-8") == "{not json"
    setup_progress.update(completed=True)
    assert _completed()
    assert not (app / setup_marker.MARKER_FILE).exists()


def test_files_recorded_for_one_folder_are_never_mixed_with_another(app, monkeypatch, tmp_path):
    _first_account()
    other = tmp_path / "other"
    other.mkdir()
    monkeypatch.setattr(config, "WORKSPACE", other)
    with pytest.raises(OSError):
        setup_marker.record_created_file("context/x.md", "0" * 64)
    assert list(_marker(app)["files"]) == [PROFILE]


def test_steps_before_the_finish_keep_the_marker(app):
    _first_account()
    setup_progress.update(step="local-consent")
    assert (app / setup_marker.MARKER_FILE).exists()


def test_editing_the_name_on_back_keeps_the_recorded_profile_matching_so_a_relaunch_still_removes_it(app):
    from infra import account_update

    _first_account()
    account_update.update_account("pat", full_name="Pat Changed", email="new@example.test")
    profile = (config.WORKSPACE / PROFILE).read_bytes()
    assert b"Pat Changed" in profile
    assert _marker(app)["files"] == {PROFILE: hashlib.sha256(profile).hexdigest()}


def test_editing_a_profile_the_person_already_had_does_not_add_it_to_the_marker(app):
    from infra import account_update

    (config.WORKSPACE / "context").mkdir()
    (config.WORKSPACE / PROFILE).write_text("# User profile\n- **Full name:** Pat\n", encoding="utf-8")
    _first_account()
    account_update.update_account("pat", full_name="Pat Changed")
    assert _marker(app)["files"] == {}


def test_a_marker_that_cannot_be_updated_undoes_the_edit_and_keeps_the_old_digest(app, monkeypatch):
    from infra import account_update

    _first_account()
    before = (config.WORKSPACE / PROFILE).read_bytes()

    def refuse(relative: str, sha256: str) -> None:
        raise OSError("marker is read-only")

    monkeypatch.setattr(setup_marker, "update_created_file", refuse)
    with pytest.raises(OSError):
        account_update.update_account("pat", full_name="Pat Changed")
    assert (config.WORKSPACE / PROFILE).read_bytes() == before
    assert _marker(app)["files"] == {PROFILE: hashlib.sha256(before).hexdigest()}
