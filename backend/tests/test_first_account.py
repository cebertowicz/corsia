"""The first account has one owner, and every way it can fail leaves nothing behind."""

from __future__ import annotations

import argparse
import io
import os
import re
import sqlite3
import sys
from contextlib import contextmanager
from pathlib import Path

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import cli_accounts  # noqa: E402
from api import setup as setup_api  # noqa: E402
from connections import signed_in_emails  # noqa: E402
from infra import account_validation, accounts, config, user_profile  # noqa: E402
from infra.default_port import DEFAULT_BACKEND_PORT  # noqa: E402
from infra.time import utc_now_iso  # noqa: E402

PASSWORD = "correct horse battery staple"
HEADERS = {"Host": f"127.0.0.1:{DEFAULT_BACKEND_PORT}", "Content-Type": "application/json"}
REQUEST = {"name": "Pat Example", "username": "pat", "password": PASSWORD, "email": "pat@example.test"}


@pytest.fixture
def workspace(isolated_db, tmp_path, monkeypatch):
    """An install with no account, a throwaway workspace and a known timezone."""
    database = accounts._db(None)
    database.execute("DELETE FROM users")
    database.commit()
    monkeypatch.setattr(config, "WORKSPACE", tmp_path)
    monkeypatch.setattr(user_profile, "mac_timezone", lambda: "America/Los_Angeles")
    return tmp_path


@pytest.fixture
def client(workspace):
    app = FastAPI()
    app.include_router(setup_api.router)
    with TestClient(
        app, base_url=f"http://127.0.0.1:{DEFAULT_BACKEND_PORT}", client=("127.0.0.1", 50100),
    ) as test_client:
        yield test_client


def _post(client, body: dict):
    return client.post("/api/setup/first-account", json=body, headers=HEADERS)


def _rows(table: str) -> int:
    return accounts._db(None).execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]


def _state() -> tuple[int, int, int]:
    """Everything an account creation adds beyond the account: its conductor, wake and tokens."""
    return _rows("users"), _rows("threads"), _rows("messages")


def _profile(workspace: Path) -> Path:
    return workspace / user_profile.PROFILE_RELATIVE_PATH


def _own_data(owner: str) -> None:
    database = accounts._db(None)
    now = utc_now_iso()
    database.execute(
        "INSERT INTO threads (id, user_id, title, created_at, updated_at) VALUES (?, ?, 'Old', ?, ?)",
        (f"t-{owner}", owner, now, now),
    )
    database.commit()


def _assert_nothing_left(response, workspace: Path, before: tuple[int, int, int], caplog) -> None:
    assert response.status_code == 500
    body = response.json()
    assert body == {"code": "setup_failed", "detail": "Your account was not created. Try again.", "ref": body["ref"]}
    assert re.fullmatch(r"[0-9ABCDEFGHJKMNPQRSTVWXYZ]{6}", body["ref"])
    assert caplog.text.count(body["ref"]) == 1
    assert _state() == before
    folder = _profile(workspace).parent
    assert not folder.exists() or list(folder.iterdir()) == []
    assert "First-account creation failed" in caplog.text
    for field in ("Pat Example", "pat@example.test"):
        assert field not in caplog.text


@pytest.mark.skipif(os.geteuid() == 0, reason="root ignores directory permissions")
def test_an_unwritable_profile_folder_creates_no_account_and_no_file(client, workspace, caplog):
    folder = _profile(workspace).parent
    folder.mkdir()
    folder.chmod(0o500)
    before = _state()
    try:
        response = _post(client, REQUEST)
    finally:
        folder.chmod(0o700)
    _assert_nothing_left(response, workspace, before, caplog)


def test_a_profile_folder_that_cannot_be_made_creates_no_account(client, workspace, caplog):
    (workspace / "context").write_text("a file where the folder belongs", encoding="utf-8")
    before = _state()
    response = _post(client, REQUEST)
    assert response.status_code == 500
    assert response.json()["code"] == "setup_failed"
    assert _state() == before
    assert (workspace / "context").read_text(encoding="utf-8") == "a file where the folder belongs"


def test_a_failing_rename_creates_no_account_and_no_file(client, workspace, monkeypatch, caplog):
    def refuse(*_args, **_kwargs):
        raise OSError("rename refused")

    monkeypatch.setattr(user_profile.os, "replace", refuse)
    before = _state()
    response = _post(client, REQUEST)
    _assert_nothing_left(response, workspace, before, caplog)


def _failing_commit(real):
    """A write_transaction whose work completes and then fails, as a commit that never lands does."""

    @contextmanager
    def wrapper(db, *, scope):
        with real(db, scope=scope) as connection:
            yield connection
            raise sqlite3.OperationalError("disk I/O error")

    return wrapper


def test_a_failed_commit_takes_the_published_profile_back(client, workspace, monkeypatch, caplog):
    monkeypatch.setattr(accounts, "write_transaction", _failing_commit(accounts.write_transaction))
    before = _state()
    response = _post(client, REQUEST)
    _assert_nothing_left(response, workspace, before, caplog)


def test_a_failed_commit_restores_an_extended_profile_exactly(client, workspace, monkeypatch, caplog):
    path = _profile(workspace)
    path.parent.mkdir()
    path.write_text("# mine\nwithout a trailing newline", encoding="utf-8")
    monkeypatch.setattr(accounts, "write_transaction", _failing_commit(accounts.write_transaction))
    before = _state()
    response = _post(client, REQUEST)
    assert response.status_code == 500
    assert _state() == before
    assert path.read_text(encoding="utf-8") == "# mine\nwithout a trailing newline"
    assert [entry.name for entry in path.parent.iterdir()] == [path.name]


@pytest.mark.parametrize(
    ("full_name", "preferred"), [("Ada Lovelace", "Ada"), ("Cher", "Cher")],
)
def test_a_new_profile_keeps_the_full_name_and_its_first_word(client, workspace, full_name, preferred):
    assert _post(client, {**REQUEST, "name": full_name, "email": None}).status_code == 200
    text = _profile(workspace).read_text(encoding="utf-8")
    assert f"- **Preferred name:** {preferred}\n- **Full name:** {full_name}\n" in text


def test_an_existing_profile_is_byte_identical_after_the_account_is_made(client, workspace):
    path = _profile(workspace)
    path.parent.mkdir()
    original = b"# mine\n- **Preferred name:** Old Name\n- **Email:** old@example.test\n"
    path.write_bytes(original)
    assert _post(client, {**REQUEST, "name": "Ada Lovelace"}).status_code == 200
    assert path.read_bytes() == original


def test_an_existing_profile_only_gains_the_email_it_names_none_of(client, workspace):
    path = _profile(workspace)
    path.parent.mkdir()
    path.write_text("# mine\n- **Preferred name:** Old Name\n", encoding="utf-8")
    assert _post(client, REQUEST).status_code == 200
    assert path.read_text(encoding="utf-8") == (
        "# mine\n- **Preferred name:** Old Name\n- **Email:** pat@example.test\n"
    )
    assert [entry.name for entry in path.parent.iterdir()] == [path.name]


def test_an_existing_profile_that_names_an_email_is_left_alone(client, workspace):
    path = _profile(workspace)
    path.parent.mkdir()
    path.write_text("- **Email:** old@example.test\n", encoding="utf-8")
    assert _post(client, REQUEST).status_code == 200
    assert path.read_text(encoding="utf-8") == "- **Email:** old@example.test\n"


def test_a_whitespace_only_name_is_a_validation_error_before_anything_is_touched(client, workspace):
    before = _state()
    response = _post(client, {**REQUEST, "name": " \t\n "})
    assert response.status_code == 422
    assert _state() == before
    assert not (workspace / "context").exists()


def test_the_account_function_refuses_a_blank_name_before_anything_is_touched(workspace):
    before = _state()
    with pytest.raises(account_validation.AccountError, match="name is required"):
        accounts.create_first_account("pat", PASSWORD, full_name="  ")
    assert _state() == before
    assert not (workspace / "context").exists()


def _cli_first_account(monkeypatch, *, name: str, answers: list[str], tty: bool = True, stdin_password: bool = False) -> int:
    queue = list(answers)
    stdin = io.StringIO(PASSWORD + "\n" if stdin_password else "")
    stdin.isatty = lambda: tty  # type: ignore[method-assign]
    monkeypatch.setattr(sys, "stdin", stdin)
    monkeypatch.setattr("builtins.input", lambda prompt="": queue.pop(0))
    monkeypatch.setattr(cli_accounts.getpass, "getpass", lambda prompt="": PASSWORD)
    monkeypatch.setattr(cli_accounts, "_ensure_schema", lambda: None)
    monkeypatch.setattr(signed_in_emails, "signed_in_emails", lambda: [])
    return cli_accounts.cmd_user_add(argparse.Namespace(name=name, password_stdin=stdin_password))


def test_the_cli_refuses_a_blank_name_without_creating_anything(workspace, monkeypatch, capsys):
    monkeypatch.setattr(cli_accounts, "_prompt_profile_name", lambda _name: "   ")
    before = _state()
    assert _cli_first_account(monkeypatch, name="pat", answers=[""]) == 2
    assert _state() == before
    assert not (workspace / "context").exists()
    assert "name is required" in capsys.readouterr().err


@pytest.mark.parametrize(("name", "status", "users"), [("someone-else", 2, 0), ("legacy-owner", 0, 1)])
def test_a_password_stdin_first_account_must_keep_existing_data(workspace, monkeypatch, capsys, name, status, users):
    _own_data("legacy-owner")
    assert _cli_first_account(monkeypatch, name=name, answers=[], tty=False, stdin_password=True) == status
    assert _rows("users") == users
    if status:
        assert "owns none of the existing data" in capsys.readouterr().err


def test_the_cli_reports_an_unwritable_profile_folder_and_creates_no_account(workspace, monkeypatch, capsys):
    (workspace / "context").write_text("not a folder", encoding="utf-8")
    before = _state()
    assert _cli_first_account(monkeypatch, name="pat", answers=["Pat Example", "pat@example.test"]) == 2
    assert _state() == before
    assert "no account was created" in capsys.readouterr().err


def test_the_cli_and_the_setup_route_write_the_same_profile(client, workspace, monkeypatch):
    assert _post(client, REQUEST).status_code == 200
    from_route = _profile(workspace).read_text(encoding="utf-8")

    database = accounts._db(None)
    database.execute("DELETE FROM users")
    database.commit()
    _profile(workspace).unlink()
    assert _cli_first_account(monkeypatch, name="pat", answers=["Pat Example", "pat@example.test"]) == 0
    assert _profile(workspace).read_text(encoding="utf-8") == from_route
    assert from_route.startswith(
        "# User profile\n\n- **Preferred name:** Pat\n- **Full name:** Pat Example\n- **Email:** pat@example.test\n"
    )


def test_cli_first_account_queues_the_script_path(workspace, monkeypatch):
    import json

    assert _cli_first_account(monkeypatch, name="pat", answers=["Pat Example", "pat@example.test"]) == 0
    [wake] = accounts._db(None).execute("SELECT metadata FROM messages WHERE id LIKE 'first-run:%'").fetchall()
    assert json.loads(wake["metadata"])["first_run_path"] == "script"


def test_staging_a_profile_changes_nothing_until_it_is_published(workspace):
    staged = user_profile.stage_user_profile(workspace, "Pat", "America/Los_Angeles", "")
    assert staged is not None
    assert not _profile(workspace).exists()
    staged.publish()
    assert _profile(workspace).read_text(encoding="utf-8").startswith("# User profile\n")
    staged.undo()
    assert not _profile(workspace).exists()
    assert list(_profile(workspace).parent.iterdir()) == []


def test_a_profile_that_appears_during_setup_is_never_overwritten(workspace):
    staged = user_profile.stage_user_profile(workspace, "Pat", "", "")
    assert staged is not None
    _profile(workspace).write_text("mine\n", encoding="utf-8")
    with pytest.raises(FileExistsError):
        staged.publish()
    staged.undo()
    assert _profile(workspace).read_text(encoding="utf-8") == "mine\n"
    assert [entry.name for entry in _profile(workspace).parent.iterdir()] == [_profile(workspace).name]
