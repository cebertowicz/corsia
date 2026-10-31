
from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
CORSIA_SCRIPT = REPO_ROOT / "scripts" / "corsia"
DELEGATE_CLI = REPO_ROOT / "backend" / "scripts" / "delegate_cli.py"

_CLOSED_BASE = "http://127.0.0.1:9"


def run_delegate(
    tmp_path: Path,
    *args: str,
    token_file: Path | None = None,
    corsia_dir: Path | None = None,
    extra_env: dict[str, str | None] | None = None,
) -> subprocess.CompletedProcess:
    tf = token_file if token_file is not None else (tmp_path / ".api-token")
    (tmp_path / "home").mkdir(exist_ok=True)
    settings = {
        "HOME": str(tmp_path / "home"),
        "PATH": os.environ.get("PATH", "/usr/bin:/bin:/usr/sbin:/sbin"),
        "CORSIA_DIR": str(corsia_dir or REPO_ROOT),
        "CORSIA_PYTHON": sys.executable,
        "CORSIA_WORKSPACE": str(tmp_path),  # skip first-run `cli init`
        "CORSIA_API_TOKEN_FILE": str(tf),
        "CORSIA_API_BASE": _CLOSED_BASE,
        **(extra_env or {}),
    }
    env = {name: value for name, value in settings.items() if value is not None}
    return subprocess.run(
        ["bash", str(CORSIA_SCRIPT), "delegate", *args],
        capture_output=True,
        text=True,
        env=env,
        timeout=60,
    )


def test_missing_token_fails_loud_and_writes_nothing(tmp_path):
    token_file = tmp_path / ".api-token"
    proc = run_delegate(tmp_path, "status", "sess-x", token_file=token_file)

    assert proc.returncode == 2, f"expected exit 2, got {proc.returncode}\n{proc.stderr}"
    assert not token_file.exists(), "must NOT materialize a token file"
    assert "No delegation API token" in proc.stderr
    assert proc.stdout.strip() == "", f"unexpected stdout: {proc.stdout!r}"
    assert "CORSIA_API_TOKEN=" in proc.stderr
    assert "--token" in proc.stderr
    assert str(token_file) in proc.stderr


def test_missing_token_never_auto_provisions_even_with_canonical_source(tmp_path):
    token_file = tmp_path / ".api-token"
    proc = run_delegate(tmp_path, "status", "sess-x", token_file=token_file)
    assert proc.returncode == 2, f"expected exit 2, got {proc.returncode}\n{proc.stderr}"
    assert not token_file.exists(), "must NOT auto-materialize the bearer to disk"
    assert "Provisioned" not in (proc.stdout + proc.stderr)


def test_present_token_file_is_honored(tmp_path):
    token_file = tmp_path / ".api-token"
    token_file.write_text("operator-supplied-token")
    os.chmod(token_file, 0o600)
    before = token_file.read_text()

    proc = run_delegate(tmp_path, "status", "sess-x", token_file=token_file)
    assert proc.returncode != 2, f"present file wrongly rejected\n{proc.stderr}"
    assert token_file.read_text() == before, "existing operator token must not be touched"


def test_empty_token_file_fails_loud(tmp_path):
    token_file = tmp_path / ".api-token"
    token_file.write_text("")  # zero bytes
    os.chmod(token_file, 0o600)

    proc = run_delegate(tmp_path, "status", "sess-x", token_file=token_file)
    assert proc.returncode == 2, f"empty file must fail loud\n{proc.stderr}"
    assert token_file.read_text() == "", "empty file must not be written to"
    assert "No delegation API token" in proc.stderr


@pytest.mark.parametrize(
    "token_args",
    [
        ("--token", "inline-tok"),  # space-separated form
        ("--token=inline-tok",),    # = form (argparse accepts both)
    ],
)
def test_explicit_token_arg_passes_guard(tmp_path, token_args):
    token_file = tmp_path / ".api-token"
    proc = run_delegate(
        tmp_path,
        *token_args,
        "status",
        "sess-x",
        token_file=token_file,
    )
    assert proc.returncode != 2, f"guard wrongly rejected --token\n{proc.stderr}"
    assert "No delegation API token" not in proc.stderr
    assert not token_file.exists(), "--token must not write a file"


def test_env_token_passes_guard(tmp_path):
    token_file = tmp_path / ".api-token"
    proc = run_delegate(
        tmp_path,
        "status",
        "sess-x",
        token_file=token_file,
        extra_env={"CORSIA_API_TOKEN": "env-tok"},
    )
    assert proc.returncode != 2, f"guard wrongly rejected env token\n{proc.stderr}"
    assert "No delegation API token" not in proc.stderr
    assert not token_file.exists()


def test_send_message_mentioning_token_still_fails_loud(tmp_path):
    token_file = tmp_path / ".api-token"
    proc = run_delegate(
        tmp_path,
        "send",
        "sess-x",
        "please use --token when you call the api",
        token_file=token_file,
    )
    assert proc.returncode == 2, f"message text wrongly passed the guard\n{proc.stderr}"
    assert not token_file.exists()
    assert "No delegation API token" in proc.stderr


def test_help_reaches_argparse_without_a_token(tmp_path):
    token_file = tmp_path / ".api-token"
    proc = run_delegate(tmp_path, "--help", token_file=token_file)
    assert proc.returncode == 0, f"--help should exit 0\n{proc.stderr}"
    assert "usage: corsia delegate" in proc.stdout
    assert "No delegation API token" not in (proc.stdout + proc.stderr)
    assert not token_file.exists()


def test_subcommand_help_reaches_argparse_without_a_token(tmp_path):
    token_file = tmp_path / ".api-token"
    proc = run_delegate(tmp_path, "create-thread", "--help", token_file=token_file)
    assert proc.returncode == 0, f"subcommand --help should exit 0\n{proc.stderr}"
    assert "No delegation API token" not in (proc.stdout + proc.stderr)
    assert not token_file.exists()


def test_bare_delegate_reaches_argparse_not_the_token_guard(tmp_path):
    token_file = tmp_path / ".api-token"
    proc = run_delegate(tmp_path, token_file=token_file)
    assert "No delegation API token" not in proc.stderr
    assert "usage: corsia delegate" in proc.stderr
    assert not token_file.exists()


def _without_override(tmp_path, *args: str) -> subprocess.CompletedProcess:
    return run_delegate(tmp_path, *args, extra_env={"CORSIA_API_TOKEN_FILE": None})


def test_without_an_override_the_token_folder_is_read(tmp_path):
    folder = tmp_path / "home" / ".corsia" / "credentials"
    folder.mkdir(parents=True, mode=0o700)
    (folder / "api-token").write_text("folder-token")

    proc = _without_override(tmp_path, "status", "sess-x")

    assert proc.returncode != 2, f"present token wrongly rejected\n{proc.stderr}"
    assert "folder-token" not in proc.stdout + proc.stderr


def test_without_an_override_an_earlier_token_moves_into_the_folder(tmp_path):
    legacy = tmp_path / "home" / ".corsia" / ".api-token"
    legacy.parent.mkdir(parents=True)
    legacy.write_text("earlier-token")

    proc = _without_override(tmp_path, "status", "sess-x")

    assert proc.returncode != 2, f"earlier token not found after the move\n{proc.stderr}"
    assert "earlier-token" not in proc.stdout + proc.stderr
    assert not legacy.exists()
    moved = tmp_path / "home" / ".corsia" / "credentials" / "api-token"
    assert moved.read_text() == "earlier-token"
    assert moved.stat().st_mode & 0o777 == 0o600


def test_without_an_override_a_missing_token_fails_loud(tmp_path):
    proc = _without_override(tmp_path, "status", "sess-x")

    assert proc.returncode == 2, f"expected exit 2, got {proc.returncode}\n{proc.stderr}"
    assert "No API token found" in proc.stderr
    assert str(tmp_path / "home" / ".corsia" / "credentials" / "api-token") in proc.stderr
    assert not (tmp_path / "home" / ".corsia" / "credentials").exists()


def test_python_cli_direct_invocation_fails_loud(tmp_path):
    token_file = tmp_path / ".api-token"
    proc = subprocess.run(
        [sys.executable, str(DELEGATE_CLI), "status", "sess-x"],
        capture_output=True,
        text=True,
        env={
            "HOME": str(tmp_path),
            "PATH": os.environ.get("PATH", "/usr/bin:/bin"),
            "CORSIA_API_TOKEN_FILE": str(token_file),
            "CORSIA_API_BASE": _CLOSED_BASE,
        },
        timeout=30,
    )
    assert proc.returncode == 2, f"expected exit 2, got {proc.returncode}\n{proc.stderr}"
    assert not token_file.exists(), "resolver must materialize nothing"
    assert "No API token found" in proc.stderr
    assert "CORSIA_API_TOKEN=" in proc.stderr
    assert "--token" in proc.stderr
    assert str(token_file) in proc.stderr
