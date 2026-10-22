from __future__ import annotations

import argparse
import io
import sys

import pytest

import cli_accounts
from connections import signed_in_emails
from infra import accounts, config, user_profile

PASSWORD = "correct horse battery staple"
PROFILE = "context/50-USER-PROFILE.md"


@pytest.fixture
def first_account(isolated_db, monkeypatch, tmp_path):
    """An install with no account, a throwaway workspace and a known Mac."""
    db = accounts._db(None)
    db.execute("DELETE FROM users")
    db.commit()
    monkeypatch.setattr(cli_accounts, "_ensure_schema", lambda: None)
    monkeypatch.setattr(config, "WORKSPACE", tmp_path)
    monkeypatch.setattr(user_profile, "mac_full_name", lambda: "Pat Example")
    monkeypatch.setattr(user_profile, "mac_timezone", lambda: "America/Los_Angeles")
    monkeypatch.setattr(cli_accounts.getpass, "getpass", lambda prompt="": PASSWORD)
    monkeypatch.setattr(signed_in_emails, "signed_in_emails", lambda: [])
    return tmp_path


def _add(monkeypatch, *, tty: bool, answer: str = "") -> int:
    stdin = io.StringIO(answer + "\n")
    stdin.isatty = lambda: tty  # type: ignore[method-assign]
    monkeypatch.setattr(sys, "stdin", stdin)
    monkeypatch.setattr("builtins.input", lambda prompt="": stdin.readline().rstrip("\n"))
    return cli_accounts.cmd_user_add(
        argparse.Namespace(name="pat", password_stdin=False)
    )


def test_profile_name_is_sanitized_and_bounded(first_account):
    path = first_account / PROFILE
    path.parent.mkdir()
    path.write_text("- **Preferred name:** Pat\x01 " + "x" * 130 + "\n", encoding="utf-8")
    assert user_profile.profile_name(first_account) == "Pat " + "x" * 116


@pytest.mark.parametrize(("reader", "label"), [
    (user_profile.uses_community_improvements, user_profile.USE_COMMUNITY_LABEL),
    (user_profile.shares_improvements, user_profile.SHARE_COMMUNITY_LABEL),
])
@pytest.mark.parametrize(("answer", "expected"), [
    ("yes", True), ("Yes.", True), ("YES, please", True),
    ("No", False), ("Not now", False), ("", False),
])
def test_community_consent_requires_an_explicit_yes(tmp_path, reader, label, answer, expected):
    profile = tmp_path / PROFILE
    profile.parent.mkdir()
    profile.write_text(f"{label} {answer}\n", encoding="utf-8")

    assert reader(tmp_path) is expected


@pytest.mark.parametrize(("reader", "label"), [
    (user_profile.uses_community_improvements, user_profile.USE_COMMUNITY_LABEL),
    (user_profile.shares_improvements, user_profile.SHARE_COMMUNITY_LABEL),
])
def test_community_consent_rejects_missing_or_misplaced_labels(tmp_path, reader, label):
    assert reader(tmp_path) is False
    profile = tmp_path / PROFILE
    profile.parent.mkdir()
    profile.write_text("- **Preferred name:** Pat\n", encoding="utf-8")

    assert reader(tmp_path) is False
    profile.write_text(f"other {label} Yes\n", encoding="utf-8")

    assert reader(tmp_path) is False


def test_the_seeded_profile_reaches_agents_as_workspace_context(first_account, monkeypatch):
    from core.workspace_context_sources import workspace_context_source_entries

    assert _add(monkeypatch, tty=True, answer="Patricia") == 0
    rows = {e.source_id: e for e in workspace_context_source_entries(first_account)}
    row = rows["workspace-context:context/50-USER-PROFILE.md"]
    assert row.inclusion == "included"
    assert "Patricia" in row.prompt_content


@pytest.mark.parametrize("name", [None, "pat"])
def test_ctrl_d_at_a_prompt_ends_with_one_line_and_no_traceback(first_account, monkeypatch, capsys, name):
    def end_of_input(prompt=""):
        raise EOFError

    stdin = io.StringIO()
    stdin.isatty = lambda: True  # type: ignore[method-assign]
    monkeypatch.setattr(sys, "stdin", stdin)
    monkeypatch.setattr("builtins.input", end_of_input)
    args = argparse.Namespace(name=name, password_stdin=False)
    assert cli_accounts.cmd_user_add(args) == 2
    err = capsys.readouterr().err.strip().splitlines()
    assert len(err) == 1 and "Traceback" not in err[0]
    assert not (first_account / PROFILE).exists()


def test_a_new_profile_holds_the_answer_and_the_macs_timezone(first_account, monkeypatch):
    assert _add(monkeypatch, tty=True, answer="Patricia") == 0
    assert (first_account / PROFILE).read_text() == (
        "# User profile\n\n"
        "- **Preferred name:** Patricia\n"
        "- **Timezone:** America/Los_Angeles. Give every time and timestamp in this zone, never UTC.\n"
    )


def test_an_empty_answer_takes_the_full_name_on_this_mac(first_account, monkeypatch):
    assert _add(monkeypatch, tty=True, answer="") == 0
    assert "- **Preferred name:** Pat Example\n" in (first_account / PROFILE).read_text()


def test_with_no_terminal_it_asks_nothing_and_uses_the_macs_full_name(first_account, monkeypatch):
    def refuse(prompt=""):
        raise AssertionError("asked a question with no terminal")

    monkeypatch.setattr("builtins.input", refuse)
    stdin = io.StringIO()
    stdin.isatty = lambda: False  # type: ignore[method-assign]
    monkeypatch.setattr(sys, "stdin", stdin)
    args = argparse.Namespace(name="pat", password_stdin=False)
    assert cli_accounts.cmd_user_add(args) == 0
    assert "- **Preferred name:** Pat Example\n" in (first_account / PROFILE).read_text()


def test_an_existing_profile_is_never_changed(first_account, monkeypatch, capsys):
    path = first_account / PROFILE
    path.parent.mkdir(parents=True)
    path.write_text("mine\n")
    assert _add(monkeypatch, tty=True, answer="Patricia") == 0
    assert path.read_text() == "mine\n"
    assert "profile" not in capsys.readouterr().out.lower()


def test_a_later_account_does_not_write_a_profile(first_account, monkeypatch):
    accounts.create_user("owner", PASSWORD)
    assert _add(monkeypatch, tty=True, answer="Patricia") == 0
    assert not (first_account / PROFILE).exists()


def test_a_scripted_account_does_not_write_a_profile(first_account, monkeypatch):
    monkeypatch.setattr(sys, "stdin", io.StringIO(PASSWORD + "\n"))
    args = argparse.Namespace(name="pat", password_stdin=True)
    assert cli_accounts.cmd_user_add(args) == 0
    assert not (first_account / PROFILE).exists()


def test_the_timezone_is_read_from_the_macs_localtime_link(tmp_path, monkeypatch):
    zone = tmp_path / "var/db/timezone/zoneinfo/America/Los_Angeles"
    zone.parent.mkdir(parents=True)
    zone.write_bytes(b"")
    link = tmp_path / "localtime"
    link.symlink_to(zone)
    monkeypatch.setattr(user_profile, "_LOCALTIME", link)
    assert user_profile.mac_timezone() == "America/Los_Angeles"
    link.unlink()
    assert user_profile.mac_timezone() == ""


def test_a_mac_that_reports_no_timezone_leaves_that_line_out():
    assert user_profile.profile_text("Pat", "") == "# User profile\n\n- **Preferred name:** Pat\n"


def _add_with_answers(monkeypatch, *answers: str) -> int:
    """Run `user add` on a terminal whose answers arrive in order: name, then email."""
    queue = list(answers)
    stdin = io.StringIO()
    stdin.isatty = lambda: True  # type: ignore[method-assign]
    monkeypatch.setattr(sys, "stdin", stdin)
    monkeypatch.setattr("builtins.input", lambda prompt="": queue.pop(0))
    return cli_accounts.cmd_user_add(argparse.Namespace(name="pat", password_stdin=False))


@pytest.mark.parametrize("answer, saved", [
    ("", "a@example.com"), ("1", "a@example.com"), ("2", "b@example.com"),
    ("c@example.com", "c@example.com"),
])
def test_the_email_choice_lands_in_the_new_profile(first_account, monkeypatch, capsys, answer, saved):
    monkeypatch.setattr(signed_in_emails, "signed_in_emails", lambda: ["a@example.com", "b@example.com"])
    assert _add_with_answers(monkeypatch, "Patricia", answer) == 0
    assert f"- **Email:** {saved}\n" in (first_account / PROFILE).read_text()
    assert "  1) a@example.com\n  2) b@example.com\n" in capsys.readouterr().out


def test_no_signed_in_email_and_an_empty_answer_saves_none(first_account, monkeypatch):
    assert _add_with_answers(monkeypatch, "Patricia", "") == 0
    assert "Email" not in (first_account / PROFILE).read_text()


def test_a_typed_email_is_saved_when_none_was_found(first_account, monkeypatch):
    assert _add_with_answers(monkeypatch, "Patricia", "me@example.com") == 0
    assert "- **Email:** me@example.com\n" in (first_account / PROFILE).read_text()


def test_something_that_is_not_an_email_is_not_saved(first_account, monkeypatch, capsys):
    assert _add_with_answers(monkeypatch, "Patricia", "nope") == 0
    assert "Email" not in (first_account / PROFILE).read_text()
    assert "not an email address" in capsys.readouterr().out


def test_with_no_terminal_no_email_is_read_or_saved(first_account, monkeypatch):
    def refuse():
        raise AssertionError("read sign-ins with no terminal")

    monkeypatch.setattr(signed_in_emails, "signed_in_emails", refuse)
    stdin = io.StringIO()
    stdin.isatty = lambda: False  # type: ignore[method-assign]
    monkeypatch.setattr(sys, "stdin", stdin)
    assert cli_accounts.cmd_user_add(argparse.Namespace(name="pat", password_stdin=False)) == 0
    assert "Email" not in (first_account / PROFILE).read_text()


def test_an_email_already_on_file_is_shown_and_kept(first_account, monkeypatch, capsys):
    path = first_account / PROFILE
    path.parent.mkdir(parents=True)
    path.write_text("mine\n- **Email:** old@example.com\n")
    monkeypatch.setattr(signed_in_emails, "signed_in_emails", lambda: ["a@example.com"])
    assert _add_with_answers(monkeypatch, "Patricia") == 0
    assert path.read_text() == "mine\n- **Email:** old@example.com\n"
    assert "Email agents use for you: old@example.com" in capsys.readouterr().out


def test_an_existing_profile_without_an_email_gets_only_that_line(first_account, monkeypatch):
    path = first_account / PROFILE
    path.parent.mkdir(parents=True)
    path.write_text("mine")
    assert _add_with_answers(monkeypatch, "Patricia", "me@example.com") == 0
    assert path.read_text() == "mine\n- **Email:** me@example.com\n"
