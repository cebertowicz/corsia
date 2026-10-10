from __future__ import annotations

import os
import re
import stat
import subprocess
import sys
from pathlib import Path
from unittest.mock import Mock

import pytest


ROOT = Path(__file__).resolve().parents[2]
SKILL_ROOT = ROOT / "backend/prompts/system/v1/skills/assistant-onboarding"
FIRST_RUN = ROOT / "backend/prompts/system/v1/skills/conductor-bootstrap/references/first-run.md"
FIRST_AGENT = FIRST_RUN.parent / "first-agent.md"
SKILL = SKILL_ROOT / "SKILL.md"
ASSETS = SKILL_ROOT / "assets"
HELPER = ASSETS / "gws-env.sh"
REPLY_GUARD = ASSETS / "gws-reply-guard.py"
RESPONSIBILITY = SKILL_ROOT / "references/assistant-responsibility.md"
GOOGLE_SETUP_SKILL = SKILL_ROOT / "references/assistant-google-setup-skill.md"
EMAIL_CHECK_SKILL = SKILL_ROOT / "references/assistant-email-check-skill.md"
GWS_SKILL = SKILL_ROOT / "references/assistant-google-workspace-skill.md"
DOCS_SKILL = SKILL_ROOT / "references/docs-safe-write-skill.md"
GOOGLE_FALLBACK = SKILL_ROOT / "references/google-cloud-setup.md"


def _run_documented_journey(
    *,
    create_thread: Mock,
    agent_roster_entry: Mock,
    agent_roster_update: Mock,
    install_instruction: Mock,
    delegate_send: Mock,
    model: str,
    account_id: str,
) -> None:
    """Mock the conductor sequence required by the shipped skill."""
    created = create_thread(
        title="Assistant", model=model, account_id=account_id, thread_kind="chat",
    )
    thread_id = created["thread_id"]
    entry = agent_roster_entry(thread_id=thread_id)
    agent_roster_update(
        thread_id=thread_id,
        expected_updated_at=entry["updated_at"],
        agent_enabled=True,
        responsibility="Email, calendar, Drive, and Docs assistance",
        responsibility_paths=[],
        responsibility_domains=["email", "calendar", "drive", "docs"],
        coordination_notes="Created through Assistant onboarding.",
    )
    attachments = (
        ("responsibility", None, RESPONSIBILITY),
        ("skill", "assistant-google-setup", GOOGLE_SETUP_SKILL),
        ("skill", "assistant-email-check", EMAIL_CHECK_SKILL),
        ("skill", "assistant-google-workspace", GWS_SKILL),
        ("skill", "docs-safe-write", DOCS_SKILL),
    )
    for kind, name, source in attachments:
        receipt = install_instruction(
            target_thread_id=thread_id,
            kind=kind,
            name=name,
            expected_revision="absent",
            content=source.read_text(encoding="utf-8"),
        )
        assert receipt["delivery"]["next_turn"] is True
    delegate_send(
        thread_id=thread_id,
        message="Run your Google setup now, then create or reuse your scheduled email check and confirm its schedule id to the user.",
    )


def test_first_run_keeps_mcp_import_and_maps_the_first_step_options():
    first_run = FIRST_RUN.read_text(encoding="utf-8") + FIRST_AGENT.read_text(encoding="utf-8")

    assert "`mcp_sources`" in first_run
    assert "/api/mcp/import" in first_run
    assert "**The Assistant:**" in first_run
    assert "never offer the Assistant" in first_run
    assert "Set up Google tools" in first_run
    assert "Connect your Google account" in first_run
    assert "Create your Assistant" in first_run
    assert "Show your first result" in first_run
    assert "<assistant_setup_node_id>" in first_run
    assert "<assistant_setup_node_revision>" in first_run
    assert "Raise the decision whose `task_title` is this task." in first_run
    assert "CORSIA builds every setup decision: raise it unchanged; never write or reword one." in first_run
    assert "Point me to a folder, or describe your work in a sentence or two." in first_run
    assert "Come back when you'd like a project too" in first_run
    assert "Come back when you'd like an agent too" in first_run


def test_task_7_is_one_handoff_and_session_consent_is_the_find_projects_decision():
    """Task 7 hands off an agent or a project; the session-read consent is its discovery decision."""
    first_run = FIRST_RUN.read_text(encoding="utf-8")

    task_7 = first_run.split("## Task 7:", 1)[1].split("## Every wake", 1)[0]
    paths = re.findall(r"^- \*\*([^:*]+):\*\*", task_7, flags=re.MULTILINE)

    assert paths == ["An agent", "A project", "Not now"]
    assert "One handoff only" in task_7
    assert "`references/first-agent.md`" in task_7
    assert "- **Not now:** close task 7" in task_7
    assert task_7.count("consent_decision_id") == 1
    assert "Only an option A that names projects is consent to read session folders." in task_7
    assert "< Back to workspace" in task_7
    assert "consent_decision`" not in first_run.replace("consent_decision_id`", "")
    assert 'append exactly `{id: "E"' not in first_run
    assert "Only answer **E**" not in first_run


def test_conductor_attaches_all_assistant_skills_then_kicks_off_once():
    create_thread = Mock(return_value={"thread_id": "assistant-1"})
    roster_entry = Mock(return_value={"updated_at": "revision-1"})
    roster_update = Mock(return_value={"changed": True})
    instruction = Mock(return_value={"delivery": {"next_turn": True}})
    kickoff = Mock()

    _run_documented_journey(
        create_thread=create_thread,
        agent_roster_entry=roster_entry,
        agent_roster_update=roster_update,
        install_instruction=instruction,
        delegate_send=kickoff,
        model="test-model",
        account_id="account-1",
    )

    assert [call.kwargs["name"] for call in instruction.call_args_list] == [
        None,
        "assistant-google-setup",
        "assistant-email-check",
        "assistant-google-workspace",
        "docs-safe-write",
    ]
    kickoff.assert_called_once_with(
        thread_id="assistant-1",
        message="Run your Google setup now, then create or reuse your scheduled email check and confirm its schedule id to the user.",
    )
    assert create_thread.call_count == 1


def test_conductor_sequence_runs_no_google_step_and_the_assistant_sequence_carries_them():
    """The Assistant owns its Google home, so only its own thread may run gws setup."""
    skill = SKILL.read_text(encoding="utf-8")
    conductor = skill[skill.index("## Create and start the Assistant"):skill.index("## Email and Docs safety")]
    setup = GOOGLE_SETUP_SKILL.read_text(encoding="utf-8")
    responsibility = RESPONSIBILITY.read_text(encoding="utf-8")

    assert "Assistant owns its Google credentials" in skill
    assert "and it comes first" in conductor
    assert "## Setup" not in skill
    for forbidden in ("gws auth login", "gws-env.sh", "request_host_access", "brew install", "agent-browser --version"):
        assert forbidden not in conductor
    assert "run no Google step" in conductor
    assert "`assistant-google-setup` from `references/assistant-google-setup-skill.md`" in conductor
    for required in ("gws --version", "agent-browser --version", "request_host_access",
                     "gws-env.sh --client-json", "gws auth login --scopes", "one read each"):
        assert required in setup
    assert "in your own thread, never the conductor's" in setup
    assert "assistant-google-setup" in responsibility
    assert "assistant-google-setup" in EMAIL_CHECK_SKILL.read_text(encoding="utf-8")


def test_google_sign_in_is_watched_by_url_and_title_only():
    """The page holds the user's password while they sign in, so only its URL and title are read."""
    setup = GOOGLE_SETUP_SKILL.read_text(encoding="utf-8")
    steps = (SKILL_ROOT.parent / "conductor-bootstrap/references/browser-person-steps.md").read_text(
        encoding="utf-8")

    assert "the browser is the user's" in setup
    assert "reading only the URL and title" in setup
    assert "never type, fill or read a password yourself" in setup
    assert "read only the page URL and title" in steps
    assert "no snapshot, screenshot, text or value read" in steps


def test_skill_keeps_revision_safe_create_and_target_owned_schedule():
    skill = SKILL.read_text(encoding="utf-8")
    email_check = EMAIL_CHECK_SKILL.read_text(encoding="utf-8")

    assert "assistant_thread_id=<thread_id>" in skill
    assert "expected_revision:<assistant_setup_node_revision>" in skill
    assert "assistant-google-workspace" in skill
    assert "docs-safe-write" in skill
    assert "Do not wait for, retry, or relay confirmation" in skill
    assert "automation_schedule" not in skill
    assert email_check.index("automation_list") < email_check.index("automation_schedule")
    assert 'payload `{ "kind": "assistant.scheduled_email_check" }`' in email_check
    assert 'spec: "3600"' in email_check


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


def test_docs_skill_pins_revision_and_requires_refusal_recovery():
    docs = DOCS_SKILL.read_text(encoding="utf-8")

    assert "writeControl.requiredRevisionId" in docs
    assert '"writeControl":{"requiredRevisionId":"<revision-id>"}' in docs
    assert "re-read the document" in docs.lower()
    assert "re-apply" in docs
    assert "ask the user" in docs


def test_fallback_and_gws_skill_use_secret_safe_handoff_and_exact_guard():
    fallback = GOOGLE_FALLBACK.read_text(encoding="utf-8")
    gws_skill = GWS_SKILL.read_text(encoding="utf-8")
    guard_spec = (SKILL_ROOT / "references/gws-reply-guardrail-spec.md").read_text(encoding="utf-8")

    assert "Download" in fallback
    assert "Source the shipped `gws-env.sh --client-json" in fallback
    assert "every numbered Cloud Console step" in fallback
    assert "never reads or prints" in fallback
    assert "source .corsia/skills/assistant-onboarding/assets/gws-env.sh" in gws_skill
    assert "Never run Homebrew from the protected agent shell" in GOOGLE_SETUP_SKILL.read_text(encoding="utf-8")
    assert "gws auth login --scopes" in gws_skill
    assert "gws-reply-guard.py --message-id" in gws_skill
    assert "G1" in guard_spec and "G6" in guard_spec and "G8" in guard_spec


def test_manifest_check_accepts_shipped_assistant_assets():
    result = subprocess.run(
        [sys.executable, "scripts/generate_system_manifest.py", "--check"],
        cwd=ROOT,
        capture_output=True,
        check=False,
        text=True,
    )

    assert result.returncode == 0, result.stdout + result.stderr


def test_person_browser_steps_have_one_rule_and_no_url_wait():
    """Captcha and sign-in handling lives in one file; `browser_wait` has no URL mode."""
    bootstrap = SKILL_ROOT.parent / "conductor-bootstrap/references"
    steps = (bootstrap / "browser-person-steps.md").read_text(encoding="utf-8")
    assert "no URL mode" in steps and "about 5 minutes" in steps
    for path in (
        bootstrap / "web-search.md",
        SKILL_ROOT.parent / "github/references/agent-account.md",
        GOOGLE_SETUP_SKILL,
    ):
        text = path.read_text(encoding="utf-8")
        assert "browser-person-steps.md" in text
        assert "`wait` on a URL" not in text
    assert "gmail.com" in SKILL.read_text(encoding="utf-8")
