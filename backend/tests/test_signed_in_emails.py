"""The emails install offers before an account exists: bounded, read-only, no database."""
from __future__ import annotations

import base64
import json
import stat
from pathlib import Path

import pytest

from connections import signed_in_emails as reader


def _claude(tmp_path: Path, monkeypatch, *, output: str, status: int = 0) -> Path:
    """A fake `claude` that prints `output`, recording how it was called."""
    calls = tmp_path / "claude-calls"
    script = tmp_path / "claude"
    script.write_text(f"#!/bin/bash\necho \"$*\" >> '{calls}'\ncat <<'EOF'\n{output}\nEOF\nexit {status}\n")
    script.chmod(script.stat().st_mode | stat.S_IEXEC)
    monkeypatch.setattr(reader, "binary_from_config", lambda provider, cfg: str(script))
    return calls


def _codex(tmp_path: Path, monkeypatch, *, email: str | None, raw: str | None = None) -> None:
    home = tmp_path / "codex-home"
    home.mkdir(exist_ok=True)
    monkeypatch.setenv("CODEX_HOME", str(home))
    if raw is not None:
        (home / "auth.json").write_text(raw)
        return
    claims: dict[str, object] = {"https://api.openai.com/auth": {"user_id": "user-1"}}
    if email:
        claims["email"] = email
    payload = base64.urlsafe_b64encode(json.dumps(claims).encode()).decode().rstrip("=")
    document = {"tokens": {"account_id": "acct-1", "id_token": f"h.{payload}.s"}}
    (home / "auth.json").write_text(json.dumps(document))


def _status(email: str, logged_in: bool = True) -> str:
    return json.dumps({"loggedIn": logged_in, "email": email})


@pytest.fixture(autouse=True)
def no_claude(monkeypatch):
    monkeypatch.setattr(reader, "binary_from_config", lambda provider, cfg: None)


def test_both_signed_in_clis_are_listed_claude_first(tmp_path, monkeypatch):
    calls = _claude(tmp_path, monkeypatch, output=_status("a@example.com"))
    _codex(tmp_path, monkeypatch, email="b@example.com")
    assert reader.signed_in_emails() == ["a@example.com", "b@example.com"]
    assert calls.read_text().strip() == "auth status"


def test_one_signed_in_cli_is_listed_alone(tmp_path, monkeypatch):
    _codex(tmp_path, monkeypatch, email="b@example.com")
    assert reader.signed_in_emails() == ["b@example.com"]
    _claude(tmp_path, monkeypatch, output=_status("a@example.com"))
    _codex(tmp_path, monkeypatch, email=None)
    assert reader.signed_in_emails() == ["a@example.com"]


def test_no_sign_in_lists_nothing(tmp_path, monkeypatch):
    _claude(tmp_path, monkeypatch, output=_status("a@example.com", logged_in=False))
    monkeypatch.setenv("CODEX_HOME", str(tmp_path / "nowhere"))
    assert reader.signed_in_emails() == []


def test_the_same_address_from_both_is_listed_once(tmp_path, monkeypatch):
    _claude(tmp_path, monkeypatch, output=_status("Pat@Example.com"))
    _codex(tmp_path, monkeypatch, email="pat@example.com")
    assert reader.signed_in_emails() == ["Pat@Example.com"]


@pytest.mark.parametrize("output, status", [("not json", 0), ("[1, 2]", 0), ('{"loggedIn": true}', 0),
                                            ('{"loggedIn": true, "email": 7}', 0), (_status("a@example.com"), 1)])
def test_malformed_or_failed_claude_output_adds_nothing(tmp_path, monkeypatch, output, status):
    _claude(tmp_path, monkeypatch, output=output, status=status)
    monkeypatch.setenv("CODEX_HOME", str(tmp_path / "nowhere"))
    assert reader.signed_in_emails() == []


@pytest.mark.parametrize("raw", ["not json", "{}", '{"tokens": {"account_id": "a", "id_token": "one-part"}}'])
def test_a_malformed_codex_auth_file_adds_nothing(tmp_path, monkeypatch, raw):
    _codex(tmp_path, monkeypatch, email=None, raw=raw)
    assert reader.signed_in_emails() == []


def test_a_missing_cli_adds_nothing_and_a_claude_that_cannot_run_is_skipped(tmp_path, monkeypatch):
    monkeypatch.setenv("CODEX_HOME", str(tmp_path / "nowhere"))
    assert reader.signed_in_emails() == []
    monkeypatch.setattr(reader, "binary_from_config", lambda provider, cfg: str(tmp_path / "gone"))
    assert reader.signed_in_emails() == []


def test_a_claude_that_hangs_is_cut_off(tmp_path, monkeypatch):
    script = tmp_path / "claude"
    script.write_text("#!/bin/bash\nexec sleep 30\n")
    script.chmod(script.stat().st_mode | stat.S_IEXEC)
    monkeypatch.setattr(reader, "binary_from_config", lambda provider, cfg: str(script))
    monkeypatch.setattr(reader, "_STATUS_SECONDS", 0.2)
    monkeypatch.setenv("CODEX_HOME", str(tmp_path / "nowhere"))
    assert reader.signed_in_emails() == []
