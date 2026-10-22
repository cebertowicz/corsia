"""An unfinished app-made setup starts over at step 1 and leaves nothing; every other install is never touched."""

from __future__ import annotations

import os
import sys
from pathlib import Path

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from api import setup as setup_api  # noqa: E402
from infra import accounts, config, setup_progress  # noqa: E402
from infra.corsia_agent import get_corsia_agent  # noqa: E402
from infra.database import get_db, write_transaction  # noqa: E402
from infra.default_port import DEFAULT_BACKEND_PORT  # noqa: E402
from infra.fresh_threads import create_fresh_thread  # noqa: E402
from infra.user_profile import PROFILE_RELATIVE_PATH  # noqa: E402
from onboarding.setup_reset import reset_unfinished_setup  # noqa: E402

PASSWORD = "[REDACTED]"
PROFILE = PROFILE_RELATIVE_PATH.as_posix()


@pytest.fixture
def workspace(isolated_db, tmp_path, monkeypatch) -> Path:
    """A disposable work folder, on an install with no account yet."""
    root = tmp_path / "work"
    root.mkdir()
    monkeypatch.setattr(config, "WORKSPACE", root)
    with write_transaction(None, scope="test_clear_accounts") as db:
        db.execute("DELETE FROM users")
    return root


def _app_account(name: str = "pat") -> str:
    """The first account the way the app's setup screen makes it."""
    return accounts.create_first_account(
        name, PASSWORD, full_name="Pat Example", email="pat@example.test", first_run_path="app",
    )


def _users() -> list[str]:
    return [row["id"] for row in get_db().execute("SELECT id FROM users")]


def _threads() -> list[str]:
    return [row["id"] for row in get_db().execute("SELECT id FROM threads")]


def _unchanged(state: dict) -> None:
    """Nothing the reset could change did change."""
    assert _users() == state["users"]
    assert sorted(_threads()) == state["threads"]
    assert setup_progress.read() == state["record"]


def _state() -> dict:
    return {"users": _users(), "threads": sorted(_threads()), "record": setup_progress.read()}


# Guard 1: the creation marker.


def test_the_app_setup_path_writes_the_creation_marker(workspace):
    user_id = _app_account()
    marker = setup_progress.read()[setup_progress.CREATED_BY_APP]
    assert marker["user_id"] == user_id
    assert marker["workspace"] == str(workspace)
    assert set(marker["files"]) == {PROFILE}


def test_an_unfinished_app_made_setup_resets_to_no_account(workspace):
    user_id = _app_account()
    result = reset_unfinished_setup(user_id)
    assert result.reset is True
    assert _users() == [] and _threads() == []
    assert setup_progress.read() is None
    assert get_corsia_agent() is None
    assert accounts.has_users() is False


def test_a_reset_install_takes_a_new_first_account_that_starts_setup_again(workspace):
    reset_unfinished_setup(_app_account())
    second = _app_account("sam")
    assert setup_progress.read()["step"] == "models"
    assert setup_progress.read()[setup_progress.CREATED_BY_APP]["user_id"] == second


def test_the_scaffold_ownership_assignment_is_undone_with_the_account(workspace):
    """Account creation records that the scaffold's documents were assigned; a reset clears it so the next account assigns them again."""
    from infra.conductor import WORKSPACE_SCOPE
    from infra.settings import get_setting, set_setting
    from registry.document_ownership import ADOPTION_SETTING_PREFIX

    key = f"{ADOPTION_SETTING_PREFIX}{WORKSPACE_SCOPE}"
    first = _app_account()
    with write_transaction(None, scope="test_assignment") as db:
        set_setting(key, {"shared_ownership_enforced_at": "2026-10-22T00:00:00+00:00"}, db=db)
    assert reset_unfinished_setup(first).reset is True
    assert get_setting(key, db=get_db()) is None


def test_a_refusal_from_the_stored_record_opens_no_write_transaction(workspace, monkeypatch):
    user_id = _app_account()
    with write_transaction(None, scope="test_complete") as db:
        setup_progress.set_setting(setup_progress.KEY, {**setup_progress.read(), "setup_completed": True}, db=db)
    from onboarding import setup_reset

    def boom(*args, **kwargs):
        raise AssertionError("a finished install must not open a write transaction")

    monkeypatch.setattr(setup_reset, "write_transaction", boom)
    assert reset_unfinished_setup(user_id).refused == "no_unfinished_setup"


def test_an_install_without_the_marker_never_resets(workspace):
    """The counterexample: one account, setup unfinished, but not made by the app (adopted, taken over, older)."""
    user_id = _app_account()
    record = setup_progress.read()
    del record[setup_progress.CREATED_BY_APP]
    with write_transaction(None, scope="test_drop_marker") as db:
        setup_progress.set_setting(setup_progress.KEY, record, db=db)
    state = _state()
    result = reset_unfinished_setup(user_id)
    assert (result.reset, result.refused) == (False, "not_created_by_app")
    _unchanged(state)
    assert (workspace / PROFILE).exists()


def test_a_script_made_account_has_no_marker_and_never_resets(workspace):
    user_id = accounts.create_first_account("pat", PASSWORD, full_name="Pat Example", first_run_path="script")
    state = _state()
    assert reset_unfinished_setup(user_id).reset is False
    _unchanged(state)


def test_a_marker_for_another_account_or_work_folder_never_resets(workspace, tmp_path, monkeypatch):
    user_id = _app_account()
    state = _state()
    assert reset_unfinished_setup("someone-else").refused == "not_created_by_app"
    monkeypatch.setattr(config, "WORKSPACE", tmp_path / "elsewhere")
    assert reset_unfinished_setup(user_id).refused == "not_created_by_app"
    _unchanged(state)


def test_a_finished_setup_never_resets(workspace):
    user_id = _app_account()
    setup_progress.update(completed=True)
    state = _state()
    assert reset_unfinished_setup(user_id).refused == "no_unfinished_setup"
    _unchanged(state)


def test_a_second_account_blocks_the_reset(workspace):
    user_id = _app_account()
    accounts.create_user("guest", PASSWORD)
    state = _state()
    assert reset_unfinished_setup(user_id).refused == "other_accounts"
    _unchanged(state)


# Guard 2: no real use yet.


def test_another_thread_blocks_the_reset(workspace):
    user_id = _app_account()
    with write_transaction(None, scope="test_other_thread") as db:
        create_fresh_thread(user_id=user_id, title="My own chat", model=None, project_id=None, db=db)
    state = _state()
    result = reset_unfinished_setup(user_id)
    assert (result.reset, result.refused) == (False, "in_use")
    _unchanged(state)


@pytest.mark.parametrize(
    ("role", "delivery_state", "delivery_error"),
    [
        ("user", "queued", "no_model_selected"),
        ("assistant", "delivered", None),
        ("system", "delivered", None),
    ],
)
def test_a_message_that_was_sent_or_read_blocks_the_reset(workspace, role, delivery_state, delivery_error):
    user_id = _app_account()
    with write_transaction(None, scope="test_message") as db:
        db.execute(
            "INSERT INTO messages (id, thread_id, role, content, created_at, delivery_state, delivery_error) "
            "VALUES ('m-real', ?, ?, 'hello', '2026-10-22T00:00:00Z', ?, ?)",
            (get_corsia_agent(), role, delivery_state, delivery_error),
        )
    state = _state()
    assert reset_unfinished_setup(user_id).refused == "in_use"
    _unchanged(state)


def test_the_held_first_run_inputs_alone_do_not_block_the_reset(workspace):
    user_id = _app_account()
    held = get_db().execute(
        "SELECT COUNT(*) FROM messages WHERE thread_id = ? AND role = 'system' AND delivery_state = 'queued' "
        "AND delivery_error = 'no_model_selected'",
        (get_corsia_agent(),),
    ).fetchone()[0]
    assert held >= 1
    assert reset_unfinished_setup(user_id).reset is True
    assert get_db().execute("SELECT COUNT(*) FROM messages").fetchone()[0] == 0


# Guard 3: the work folder is untouched.


def test_the_profile_goes_only_while_it_is_unchanged(workspace):
    user_id = _app_account()
    assert (workspace / PROFILE).is_file()
    reset_unfinished_setup(user_id)
    assert not (workspace / PROFILE).exists()


def test_an_edited_profile_stays(workspace):
    user_id = _app_account()
    with (workspace / PROFILE).open("a", encoding="utf-8") as profile:
        profile.write("- **Likes:** short answers\n")
    assert reset_unfinished_setup(user_id).reset is True
    assert "short answers" in (workspace / PROFILE).read_text(encoding="utf-8")


def test_a_profile_the_person_already_had_stays(workspace):
    (workspace / "context").mkdir()
    (workspace / PROFILE).write_text("# Mine\n- **Preferred name:** Pat\n", encoding="utf-8")
    user_id = _app_account()
    assert PROFILE not in setup_progress.read()[setup_progress.CREATED_BY_APP]["files"]
    assert reset_unfinished_setup(user_id).reset is True
    assert (workspace / PROFILE).read_text(encoding="utf-8").startswith("# Mine")


def test_nothing_else_in_the_work_folder_is_touched(workspace):
    user_id = _app_account()
    (workspace / "notes.md").write_text("mine", encoding="utf-8")
    (workspace / "context" / "other.md").write_text("mine too", encoding="utf-8")
    (workspace / "projects").mkdir()
    (workspace / "projects" / "idea.txt").write_text("keep", encoding="utf-8")
    reset_unfinished_setup(user_id)
    assert sorted(path.relative_to(workspace).as_posix() for path in workspace.rglob("*")) == [
        "context", "context/other.md", "notes.md", "projects", "projects/idea.txt",
    ]


def test_a_symlinked_profile_is_never_followed(workspace, tmp_path):
    user_id = _app_account()
    target = tmp_path / "outside.md"
    target.write_bytes((workspace / PROFILE).read_bytes())
    (workspace / PROFILE).unlink()
    (workspace / PROFILE).symlink_to(target)
    reset_unfinished_setup(user_id)
    assert target.exists()


# Guard 4: installed tools stay.


def test_installed_tools_and_downloads_stay(workspace):
    from connections.models import get_connection

    user_id = _app_account()
    before = {
        "connections": get_db().execute("SELECT COUNT(*) FROM connections").fetchone()[0],
        "models": get_db().execute("SELECT COUNT(*) FROM models").fetchone()[0],
        "claude-code": get_connection("claude-code"),
    }
    reset_unfinished_setup(user_id)
    assert get_db().execute("SELECT COUNT(*) FROM connections").fetchone()[0] == before["connections"]
    assert get_db().execute("SELECT COUNT(*) FROM models").fetchone()[0] == before["models"]
    assert get_connection("claude-code") == before["claude-code"]


# The route.


@pytest.fixture
def client(workspace):
    app = FastAPI()
    app.include_router(setup_api.router)
    return TestClient(
        app, base_url="http:" + f"//127.0.0.1:{DEFAULT_BACKEND_PORT}", client=("127.0.0.1", 50100),
    )


def test_the_route_resets_for_the_signed_in_person(client):
    user_id = _app_account()
    session, _expires = accounts.issue_token(user_id, kind="session", label="browser")
    response = client.post("/api/setup/reset-unfinished", headers={"Authorization": f"Bearer {session}"})
    assert response.status_code == 200
    assert response.json()["reset"] is True
    assert _users() == []


def test_the_route_refuses_a_machine_token_and_a_stranger(client):
    user_id = _app_account()
    api_token, _expires = accounts.issue_token(user_id, kind="api", label="tool")
    state = _state()
    assert client.post("/api/setup/reset-unfinished", headers={"Authorization": f"Bearer {api_token}"}).status_code == 403
    assert client.post("/api/setup/reset-unfinished").status_code == 401
    assert client.post("/api/setup/reset-unfinished", params={"token": api_token}).status_code == 401
    _unchanged(state)
