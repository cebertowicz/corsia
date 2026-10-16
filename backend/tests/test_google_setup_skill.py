from __future__ import annotations

import re
from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]
SKILLS = ROOT / "backend/prompts/system/v1/skills"
SKILL = SKILLS / "google-setup/SKILL.md"
FIRST_RUN = SKILLS / "conductor-bootstrap/references/first-run.md"
FIRST_RUN_APP = FIRST_RUN.parent / "first-run-app.md"
FIRST_AGENT = FIRST_RUN.parent / "first-agent.md"
IMPORT_RESULT_LINES = FIRST_RUN.parent / "import-result-lines.md"


def test_first_run_keeps_mcp_import_and_maps_the_first_step_options():
    first_run = FIRST_RUN.read_text(encoding="utf-8") + FIRST_AGENT.read_text(encoding="utf-8")

    assert "`mcp_sources`" in first_run
    assert "/api/mcp/import" in first_run
    assert "Bring over all [n] apps listed above, some (tell me which), or none?" in first_run
    assert "with only the pairs they approved" in first_run
    assert "`references/import-result-lines.md`" in first_run
    assert "`Okay, nothing brought over.`" in first_run
    result_lines = IMPORT_RESULT_LINES.read_text(encoding="utf-8")
    for status in ("awaiting_approval", "already_exists", "outside_project", "cannot_connect", "failed"):
        assert f"`{status}`" in result_lines or f'"{status}"' in result_lines
    assert "`Approve them in [Settings › System › Connected apps](/settings/system#connected-apps).`" in result_lines
    assert "Email and calendar on a Google account are yours, not an agent's" in first_run
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


def test_google_sign_in_is_watched_by_url_and_title_only():
    """The page holds the user's password while they sign in, so only its URL and title are read."""
    steps = (SKILLS / "conductor-bootstrap/references/browser-person-steps.md").read_text(encoding="utf-8")

    assert "read only the page URL and title" in steps
    assert "no snapshot, screenshot, text or value read" in steps


def test_person_browser_steps_have_one_rule_and_no_url_wait():
    """Captcha and sign-in handling lives in one file; `browser_wait` has no URL mode."""
    bootstrap = SKILLS / "conductor-bootstrap/references"
    steps = (bootstrap / "browser-person-steps.md").read_text(encoding="utf-8")
    assert "no URL mode" in steps and "about 5 minutes" in steps
    for path in (
        bootstrap / "web-search.md",
        SKILLS / "github/references/agent-account.md",
        SKILL,
    ):
        text = path.read_text(encoding="utf-8")
        assert "browser-person-steps.md" in text
        assert "`wait` on a URL" not in text


def test_email_and_calendar_route_to_google_setup():
    """Step 6 and the first-agent path both load google-setup; another mail provider still gets an agent."""
    assert "skill `google-setup`" in FIRST_RUN_APP.read_text(encoding="utf-8")
    first_agent = FIRST_AGENT.read_text(encoding="utf-8")
    assert "connect them with skill `google-setup` instead of creating an agent" in first_agent
    assert "another mail provider included, goes to step 3" in first_agent
    assert "When it ends, connected or not now, close task 7" in first_agent
    task_7 = FIRST_RUN.read_text(encoding="utf-8").split("## Task 7:", 1)[1].split("## Every wake", 1)[0]
    assert "or `google-setup` ends for an email answer" in task_7


def test_google_setup_carries_the_approved_offer_and_leaves_secrets_to_the_backend():
    skill = SKILL.read_text(encoding="utf-8")
    steps = skill.split("# Connect Google", 1)[1]

    assert "I'll set up your own private Google project in a browser you can watch" in steps
    assert "Google will also ask for your Drive, Docs and Sheets, so you won't have to sign in again later." in steps
    for api in ("Gmail API", "Google Calendar API", "Google Drive API", "Google Docs API", "Google Sheets API"):
        assert api in steps
    assert '"Go to CORSIA (unsafe)"' in steps
    assert "a project left in Testing stops letting me in after 7 days. Go ahead?" in steps
    assert "`Ask me any time to connect your email.`" in steps
    assert "go on only when it says In production" in steps
    assert "browser-person-steps.md` B" in steps
    assert "Never make a second project or client." in steps
    assert "shows the client secret, so take no snapshot or screenshot of it" in steps
    assert "Read the client ID from the Clients list" in steps and "never open it" in steps
    assert steps.index("`google_connect` with no `client_file`") < steps.index("an error that says to install agent-browser")
    assert "whatever the cause it names" in steps
    assert "exactly the install commands that error names" in steps
    assert "then call `browser_open` again" in steps
    assert "@0." not in steps
    assert "Connected as <address>. If that's the wrong account, say so and I'll switch it." in steps
