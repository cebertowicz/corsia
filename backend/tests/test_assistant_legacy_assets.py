"""Assets kept for Assistants created before v0.4.0.

Those agents' attached skills source `.corsia/skills/assistant-onboarding/assets/`
by that exact path, so the two helpers stay there with no SKILL.md beside them.
"""
from __future__ import annotations

import os
import re
import stat
import subprocess
import sys
from pathlib import Path

import pytest


ROOT = Path(__file__).resolve().parents[2]
ASSETS = ROOT / "backend/prompts/system/v1/skills/assistant-onboarding/assets"
HELPER = ASSETS / "gws-env.sh"
REPLY_GUARD = ASSETS / "gws-reply-guard.py"


def test_only_the_assets_remain_so_no_new_assistant_skill_is_offered():
    assert sorted(path.name for path in ASSETS.parent.rglob("*") if path.is_file()) == [
        "gws-env.sh", "gws-reply-guard.py",
    ]


def test_helper_moves_client_sets_environment_and_never_echoes_values(tmp_path: Path):
    home = tmp_path / "gws-home"
    home.mkdir()
    client = tmp_path / "desktop-client.json"
    client.write_text(
        '{"installed":{"client_id":"client-value","client_secret":"secret-value"}}',
        encoding="utf-8",
    )
    command = (
        'source "$1" --client-json "$2"; '
        'test "$SSL_CERT_FILE" = /etc/ssl/cert.pem; '
        'test "$GOOGLE_WORKSPACE_CLI_KEYRING_BACKEND" = file; '
        'test -d "$GOOGLE_WORKSPACE_CLI_CONFIG_DIR"; '
        'test -d "$TMPDIR"'
    )
    environment = {"CORSIA_GWS_HOME": str(home), "PATH": os.environ["PATH"]}

    result = subprocess.run(
        ["bash", "-c", command, "bash", str(HELPER), str(client)],
        capture_output=True,
        check=False,
        env=environment,
        text=True,
    )

    stored = home / "config/desktop-client.json"
    assert result.returncode == 0
    assert result.stdout == ""
    assert result.stderr == ""
    assert not client.exists()
    assert stat.S_IMODE(stored.stat().st_mode) == 0o600
    for directory in (home, home / "config", home / "tmp"):
        assert stat.S_IMODE(directory.stat().st_mode) == 0o700


@pytest.mark.parametrize("arguments", [["--bogus"], ["--client-json"], ["--client-json", "/nonexistent"]])
def test_helper_failure_stops_the_sourcing_shell_step(tmp_path: Path, arguments: list[str]):
    """A failed check must return from `source`, not loop or run the next step."""
    home = tmp_path / "gws-home"
    home.mkdir()
    command = 'source "$@"; echo "after-source $?"'
    environment = {"CORSIA_GWS_HOME": str(home), "PATH": os.environ["PATH"]}

    result = subprocess.run(
        ["bash", "-c", command, "bash", str(HELPER), *arguments],
        capture_output=True,
        check=False,
        env=environment,
        text=True,
        timeout=10,
    )

    assert result.stdout == "after-source 1\n"
    assert result.stderr == "Unable to prepare the Google Workspace environment.\n"
    assert not (home / "config/desktop-client.json").exists()


def _client_file(tmp_path: Path) -> Path:
    client = tmp_path / "desktop-client.json"
    client.write_text('{"installed":{"client_id":"client-value","client_secret":"secret-value"}}', encoding="utf-8")
    return client


@pytest.mark.parametrize("home", [None, "", "relative/home", "MISSING"])
def test_helper_without_a_usable_gws_home_fails_closed(tmp_path: Path, home: str | None):
    """No CORSIA_GWS_HOME, a relative one or a missing directory: nothing is created anywhere."""
    environment = {"PATH": os.environ["PATH"]}
    if home == "MISSING":
        environment["CORSIA_GWS_HOME"] = str(tmp_path / "absent")
    elif home is not None:
        environment["CORSIA_GWS_HOME"] = home
    cwd = tmp_path / "cwd"
    cwd.mkdir()
    client = _client_file(tmp_path)

    result = subprocess.run(
        ["bash", "-c", 'source "$1" --client-json "$2"; echo "after-source $?"', "bash", str(HELPER), str(client)],
        capture_output=True, check=False, cwd=cwd, env=environment, text=True,
    )

    assert result.stdout == "after-source 1\n"
    assert result.stderr
    assert list(cwd.iterdir()) == []
    assert not (tmp_path / "absent").exists()
    assert client.exists()


def _old_store(workspace: Path) -> Path:
    config = workspace / ".corsia-assistant/gws/config"
    config.mkdir(parents=True)
    (config / "desktop-client.json").write_text('{"installed":{"client_id":"a","client_secret":"b"}}', encoding="utf-8")
    (config / "credentials.enc").write_text("token-bytes", encoding="utf-8")
    (workspace / ".corsia-assistant/gws/tmp").mkdir()
    return workspace / ".corsia-assistant"


def _source_helper(helper: Path, home: Path, cwd: Path, **extra: str) -> subprocess.CompletedProcess[str]:
    environment = {"CORSIA_GWS_HOME": str(home), "PATH": os.environ["PATH"], **extra}
    return subprocess.run(
        ["bash", "-c", 'helper=$1; set --; source "$helper"; echo "after-source $?"', "bash", str(helper)],
        capture_output=True, check=False, cwd=cwd, env=environment, text=True,
    )


def test_helper_moves_an_in_workspace_store_once_and_deletes_the_old_copy(tmp_path: Path):
    workspace, home = tmp_path / "workspace", tmp_path / "gws-home"
    home.mkdir()
    old = _old_store(workspace)
    placed = workspace / ".corsia/skills/assistant-onboarding/assets/gws-env.sh"
    placed.parent.mkdir(parents=True)
    placed.write_text(HELPER.read_text(encoding="utf-8"), encoding="utf-8")
    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir()

    # The first run has no client-json argument and stops after the move; only the move matters.
    first = _source_helper(placed, home, elsewhere)

    assert first.stdout == "after-source 0\n"
    assert (home / "config/credentials.enc").read_text(encoding="utf-8") == "token-bytes"
    assert (home / "config/desktop-client.json").exists()
    assert not old.exists()

    again = _source_helper(placed, home, elsewhere)
    assert again.stdout == "after-source 0\n"
    assert (home / "config/credentials.enc").read_text(encoding="utf-8") == "token-bytes"


def test_helper_keeps_the_old_store_when_the_new_home_already_has_one(tmp_path: Path):
    workspace, home = tmp_path / "workspace", tmp_path / "gws-home"
    (home / "config").mkdir(parents=True)
    (home / "config/credentials.enc").write_text("new-token", encoding="utf-8")
    old = _old_store(workspace)

    _source_helper(HELPER, home, tmp_path, CORSIA_WORKSPACE=str(workspace))

    assert (home / "config/credentials.enc").read_text(encoding="utf-8") == "new-token"
    assert (old / "gws/config/credentials.enc").exists()


def test_reply_guard_previews_then_sends_current_document_text(tmp_path: Path):
    log = tmp_path / "gws.log"
    fake_gws = tmp_path / "gws"
    fake_gws.write_text(
        "#!/usr/bin/env bash\n"
        "case \"$*\" in\n"
        "  *'messages get'*) if [[ \"$*\" == *parallel-1* ]]; then echo '{\"id\":\"parallel-1\",\"threadId\":\"thread-2\",\"internalDate\":\"2\",\"payload\":{\"headers\":[{\"name\":\"From\",\"value\":\"Sender <sender@example.test>\"},{\"name\":\"Subject\",\"value\":\"Re: Status\"}]}}'; else echo '{\"id\":\"message-1\",\"threadId\":\"thread-1\",\"internalDate\":\"1\",\"payload\":{\"headers\":[{\"name\":\"From\",\"value\":\"Sender <sender@example.test>\"},{\"name\":\"Subject\",\"value\":\"Status\"}]}}'; fi ;;\n"
        "  *'threads get'*) echo '{\"messages\":[{\"id\":\"message-1\",\"threadId\":\"thread-1\",\"internalDate\":\"1\",\"payload\":{\"headers\":[{\"name\":\"From\",\"value\":\"Sender <sender@example.test>\"}]}}]}' ;;\n"
        "  *'getProfile'*) echo '{\"emailAddress\":\"assistant@example.test\"}' ;;\n"
        "  *'messages list'*) if [ \"${GWS_PARALLEL:-0}\" = 1 ]; then echo '{\"messages\":[{\"id\":\"parallel-1\",\"threadId\":\"thread-2\"}]}'; else echo '{}'; fi ;;\n"
        "  *'gmail +reply'*) printf '%s\\n' \"$*\" >> \"$GWS_LOG\"; echo '{}' ;;\n"
        "  *) exit 2 ;;\n"
        "esac\n",
        encoding="utf-8",
    )
    fake_gws.chmod(0o755)
    current_text = tmp_path / "current-document.txt"
    current_text.write_text("Current draft text", encoding="utf-8")
    environment = {"PATH": f"{tmp_path}:{os.environ['PATH']}", "GWS_LOG": str(log)}

    result = subprocess.run(
        [sys.executable, str(REPLY_GUARD), "--message-id", "message-1", "--body-file", str(current_text), "--send"],
        capture_output=True,
        check=False,
        env=environment,
        text=True,
    )

    invocations = log.read_text(encoding="utf-8").splitlines()
    assert result.returncode == 0
    assert len(invocations) == 2
    assert "--dry-run" in invocations[0]
    assert "--dry-run" not in invocations[1]
    assert "Current draft text" in invocations[1]


def test_reply_guard_blocks_hydrated_parallel_message_before_preview(tmp_path: Path):
    log = tmp_path / "gws.log"
    fake_gws = tmp_path / "gws"
    fake_gws.write_text(
        "#!/usr/bin/env bash\n"
        "case \"$*\" in\n"
        "  *'messages get'*) if [[ \"$*\" == *parallel-1* ]]; then echo '{\"id\":\"parallel-1\",\"threadId\":\"thread-2\",\"internalDate\":\"2\",\"payload\":{\"headers\":[{\"name\":\"From\",\"value\":\"Sender <sender@example.test>\"},{\"name\":\"Subject\",\"value\":\"Re: Status\"}]}}'; else echo '{\"id\":\"message-1\",\"threadId\":\"thread-1\",\"internalDate\":\"1\",\"payload\":{\"headers\":[{\"name\":\"From\",\"value\":\"Sender <sender@example.test>\"},{\"name\":\"Subject\",\"value\":\"Status\"}]}}'; fi ;;\n"
        "  *'threads get'*) echo '{\"messages\":[{\"id\":\"message-1\",\"threadId\":\"thread-1\",\"internalDate\":\"1\",\"payload\":{\"headers\":[{\"name\":\"From\",\"value\":\"Sender <sender@example.test>\"}]}}]}' ;;\n"
        "  *'getProfile'*) echo '{\"emailAddress\":\"assistant@example.test\"}' ;;\n"
        "  *'messages list'*) echo '{\"messages\":[{\"id\":\"parallel-1\",\"threadId\":\"thread-2\"}]}' ;;\n"
        "  *'gmail +reply'*) printf '%s\\n' \"$*\" >> \"$GWS_LOG\"; echo '{}' ;;\n"
        "  *) exit 2 ;;\n"
        "esac\n",
        encoding="utf-8",
    )
    fake_gws.chmod(0o755)
    body = tmp_path / "current-document.txt"
    body.write_text("Current draft text", encoding="utf-8")

    result = subprocess.run(
        [sys.executable, str(REPLY_GUARD), "--message-id", "message-1", "--body-file", str(body), "--send"],
        capture_output=True,
        check=False,
        env={"PATH": f"{tmp_path}:{os.environ['PATH']}", "GWS_LOG": str(log)},
        text=True,
    )

    assert result.returncode == 3
    assert "FAIL G6" in result.stderr
    assert not log.exists()
