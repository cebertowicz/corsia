from __future__ import annotations

import re
from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]
SKILLS = ROOT / "backend/prompts/system/v1/skills"
SKILL = SKILLS / "google-setup/SKILL.md"
FIRST_RUN = SKILLS / "conductor-bootstrap/references/first-run.md"
FIRST_RUN_APP = FIRST_RUN.parent / "first-run-app.md"
IMPORT_RESULT_LINES = FIRST_RUN.parent / "import-result-lines.md"


def test_first_run_keeps_mcp_import_and_maps_the_first_step_options():
    first_run = FIRST_RUN.read_text(encoding="utf-8")

    assert "`mcp_sources`" in first_run
    assert "/api/mcp/import" in first_run
    assert "Bring over all [n] apps listed above, some (tell me which), or none?" in first_run
    assert "with only the pairs they approved" in first_run
    assert "`references/import-result-lines.md`" in first_run
    assert "`Okay, nothing brought over.`" in first_run
    result_lines = IMPORT_RESULT_LINES.read_text(encoding="utf-8")
    for status in ("awaiting_approval", "already_exists", "outside_project", "cannot_connect", "failed"):
        assert f"`{status}`" in result_lines or f'"{status}"' in result_lines
    assert "`Approve them in [Settings › Connected apps](/settings/connected-apps).`" in result_lines
    assert "Raise the decision whose `task_title` is this task." in first_run
    assert "CORSIA builds every setup decision: raise it unchanged; never write or reword one." in first_run
    assert "Point me to a folder, or describe your work in a sentence or two." in first_run
    assert "You can see those numbers on the Measurement dashboard." in first_run.split("## Wrap-up", 1)[1]


def test_task_7_offers_only_a_project_and_session_consent_is_the_find_projects_decision():
    """Setup offers no agent; the session-read consent is task 7's discovery decision."""
    first_run = FIRST_RUN.read_text(encoding="utf-8")

    task_7 = first_run.split("## Task 7:", 1)[1].split("## Every wake", 1)[0]
    paths = re.findall(r"^- \*\*([^:*]+):\*\*", task_7, flags=re.MULTILINE)

    assert paths == ["Yes", "Not now"]
    assert "Setup offers no agent" in task_7
    assert "## Task 7: Set up your first project" in first_run
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


def test_email_and_calendar_route_to_google_setup_and_setup_hands_off_no_agent():
    """Step 6 loads google-setup without creating an agent, and the hand-off offers none."""
    app = FIRST_RUN_APP.read_text(encoding="utf-8")
    assert "follow skill `google-setup` from its step 1" in app and "it creates no agent" in app
    hand_off = app.split("## The hand-off", 1)[1]
    assert "Setup offers no agent" in hand_off
    assert "only when they ask you to take that work off them (skill `time-to-agent`)" in hand_off


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
    assert steps.count("console.cloud.google.com/projectcreate") == 1
    assert "5. In the window step 4 opened" in steps
    assert "whatever the cause it names" in steps
    assert "exactly the install commands that error names" in steps
    assert "then call `browser_open` again" in steps
    assert "@0." not in steps
    assert "Connected as <address>. If that's the wrong account, say so and I'll switch it." in steps
