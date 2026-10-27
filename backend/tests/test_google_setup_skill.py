from __future__ import annotations

import json
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
    for status in ("awaiting_approval", "already_exists", "cannot_connect", "failed"):
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


def test_google_sign_in_uses_the_observed_person_step():
    """The page holds the user's password, and CORSIA resumes only after page navigation."""
    steps = (SKILLS / "conductor-bootstrap/references/browser-person-steps.md").read_text(encoding="utf-8")

    sign_in = steps.split("## B.", 1)[1].split("## C.", 1)[0]
    assert "`browser_person_step`" in sign_in
    assert "end your turn" in sign_in
    assert "wakes you when the page changes" in sign_in
    assert "Never type, fill or read a password or a code yourself." in sign_in


def test_person_browser_steps_have_one_rule_and_no_url_wait():
    """Captcha and sign-in handling lives in one file; `browser_wait` has no URL mode."""
    bootstrap = SKILLS / "conductor-bootstrap/references"
    steps = (bootstrap / "browser-person-steps.md").read_text(encoding="utf-8")
    assert "no URL mode" in steps and "about 5 minutes" in steps
    for path in (
        bootstrap / "web-search.md",
        SKILL,
    ):
        text = path.read_text(encoding="utf-8")
        assert "browser-person-steps.md" in text
        assert "`wait` on a URL" not in text


def test_github_sign_up_is_the_persons_own_browser_never_the_shared_one():
    """GitHub flags a sign-up an automated browser types, so the person signs up themselves."""
    account = (SKILLS / "github/references/agent-account.md").read_text(encoding="utf-8")
    assert "never open the sign-up in the shared browser and never type into or click its form" in account
    assert "paste this link into it: https://github.com/signup" in account
    assert "`browser_open`" not in account.split("## Creating the account", 1)[1].split("## Posting as the person", 1)[0]


def test_person_browser_guidance_keeps_the_other_first_run_steps_in_the_shared_browser():
    system = ROOT / "backend/prompts/system/v1"
    expected_guidance = {
        "skills/conductor-bootstrap/references/browser-person-steps.md": (
            "Use this whenever you open a page in the shared browser (`browser_open` with `show_window: true`)"
        ),
        "skills/conductor-bootstrap/references/web-search.md": (
            "`browser_open` and `show_window: true`; the person watches in the shared browser"
        ),
        "skills/conductor-bootstrap/references/first-run.md": (
            "the person signs up in their own browser from the link you post"
        ),
        "skills/google-setup/SKILL.md": (
            "`browser_open` and `show_window: true`, so the person watches it in the shared browser."
        ),
        "skills/microsoft-setup/SKILL.md": (
            "`browser_open` and `show_window: true`; the person watches in the shared browser"
        ),
    }
    for relative_path, instruction in expected_guidance.items():
        assert instruction in (system / relative_path).read_text(encoding="utf-8")


def test_email_and_calendar_route_to_google_setup_and_setup_hands_off_no_agent():
    """Step 5 loads google-setup without creating an agent, and the hand-off offers none."""
    app = FIRST_RUN_APP.read_text(encoding="utf-8")
    assert "follow skill `google-setup` from its step 2" in app and "it creates no agent" in app
    hand_off = app.split("## Hand over to your agents", 1)[1]
    assert "Setup offers no agent" in hand_off
    assert "only when they ask you to take that work off them (skill `time-to-agent`)" in hand_off


def test_google_setup_carries_the_approved_offer_and_leaves_secrets_to_the_backend():
    skill = SKILL.read_text(encoding="utf-8")
    steps = skill.split("# Connect Google", 1)[1]

    assert "CORSIA creates a private Google project you own, in a browser you can watch" in steps
    assert "`See, edit and delete your Drive, Docs and Sheets files, so you won't have to sign in again later.`" in steps
    # The console clicks live in the server-owned step file; the skill names that file.
    assert '`steps: "google-setup/console-project"`' in steps
    assert '`steps: "google-setup/console-apis"`' in steps
    project = json.loads((SKILLS / "google-setup/steps/console-project.json").read_text(encoding="utf-8"))
    apis = json.loads((SKILLS / "google-setup/steps/console-apis.json").read_text(encoding="utf-8"))
    assert [step["id"] for step in project["steps"]] == ["project-page", "project-name", "project-create", "project-ready"]
    enable_link = "https://console.cloud.google.com/flows/enableapi?apiid="
    urls = [step["url"] for step in apis["steps"] if step.get("url", "").startswith(enable_link)]
    assert len(urls) == 1
    enabled = urls[0].removeprefix(enable_link).split(",")
    assert enabled == ["gmail.googleapis.com", "calendar-json.googleapis.com", "drive.googleapis.com",
                       "docs.googleapis.com", "sheets.googleapis.com"]
    assert '"Go to CORSIA (unsafe)"' in steps
    assert (
        "a project left in Testing stops letting CORSIA in after 7 days.` then "
        "`Which Google accounts should I connect? List each address and I'll start.`"
    ) in steps
    assert "`Ask me any time to connect your email.`" in steps
    assert "go on only when it says In production" in steps
    assert "browser-person-steps.md` B" in steps
    assert "Never make a second project." in steps
    assert "shows the client secret may still be open, so take no snapshot or screenshot of it" in steps
    assert "Read the new client's ID from the Clients list" in steps and "latest creation date" in steps and "never open it" in steps
    assert steps.index("`google_connect` with no `client_file`") < steps.index("If the error says to reinstall or update CORSIA")
    assert steps.count("console.cloud.google.com/projectcreate") == 1
    assert "`browser_open` and `show_window: true`, so the person watches it in the shared browser." in steps
    assert "Do not revisit `projectcreate` or submit Create again" in steps
    assert "wait for the project selector to show `CORSIA` as the current project" in steps
    assert "first check whether the selector lists `CORSIA`: select it if it does" in steps
    assert "5. In the shared browser from step 4:" in steps
    assert "report it the way you report any problem with CORSIA, and stop" in steps
    assert "If it names install commands instead (CORSIA run from source)" in steps
    assert "then call `browser_open` again" in steps
    assert "@0." not in steps
    assert "Post `Connected as <address>.` for each" in steps
    assert "`If that's the wrong account, say so and I'll switch it.` once" in steps
    assert "If the person means an account listed there, ask which one first when more than one could be meant" in steps
    assert "if that account shows `needs_action`, follow that instead of this skill" in steps
    assert "Otherwise call `google_connect` with that `account_id`" in steps
    assert "post its `reply` verbatim with `post_update` followed by `[Open it in Connected apps](<link>)`" in steps
    assert "To connect a new account, including the first, go on to step 2." in steps


def test_google_setup_asks_for_accounts_does_the_console_and_checks_scopes():
    """Several accounts in one run: the audience fits them all, the agent does the console, links go out together."""
    from connections.google.oauth import OPTIONAL_SCOPES, REQUIRED_SCOPES

    steps = SKILL.read_text(encoding="utf-8")
    step_2, step_3 = steps.split("\n2. ", 1)[1].split("\n3. ", 1)
    step_5 = step_3.split("\n5. ", 1)[1].split("\n6. ", 1)[0]
    assert "Which Google accounts should I connect?" in step_2
    assert step_5.index("every address is in the signed-in account's domain; otherwise External") < step_5.index("press Publish app")
    # A saved client skips the console, but an Internal project would refuse an address from another domain.
    assert "press Make external and confirm, do step 5c, then go to step 6" in step_3
    assert "gmail.com counts as a domain of its own" in step_3
    # No hand-offs for the console: only a step that still fails after a retry, reported first.
    assert "never send the person to the console or hand them a list" in steps
    assert "If a console step still fails after one retry, report it" in step_5
    # Google shows a secret only at creation, so an existing client gets a new one beside it.
    assert "Run it even when the Clients list already has a `CORSIA` client" in step_5
    assert "Leave the old client alone" in step_5
    # The secret dialog stays open through the steps after download-json, until the reload.
    assert "stops at `download-json` or a later step" in step_5
    assert "call again once with `start_at` set to the step it stopped at" in step_5
    # A rerun keeps an earlier run's saved branding instead of skipping it.
    assert "If it already shows app name `CORSIA`, an earlier run saved it" in step_5
    # One post with every link; every permission box ticked; the granted scopes read back.
    assert "Post all the links in one `post_update`" in steps
    assert "tick every box before you continue" in steps
    for scope in REQUIRED_SCOPES + OPTIONAL_SCOPES:
        assert f"`{scope.rsplit('/', 1)[1].split('.', 1)[0]}`" in steps.split("\n7. ", 1)[1]
    assert "To add them, remove <address> in Connected apps, then ask me to connect it again and tick every box." in steps.split("\n7. ", 1)[1]


def test_setup_skills_tell_a_packaged_apps_browser_error_from_a_source_install(tmp_path, monkeypatch):
    from host_access import browser_cli

    monkeypatch.setattr(browser_cli, "app_bundle_root", lambda: tmp_path)
    packaged = browser_cli.install_hint()
    monkeypatch.setattr(browser_cli, "app_bundle_root", lambda: None)
    source = browser_cli.install_hint()
    assert "reinstall or update CORSIA" in packaged and "reinstall or update CORSIA" not in source
    for skill in (SKILL, SKILLS / "microsoft-setup/SKILL.md"):
        text = skill.read_text(encoding="utf-8")
        assert "If the error says to reinstall or update CORSIA, the app's own browser is broken, a gap in CORSIA" in text
        assert "report it the way you report any problem with CORSIA, and stop" in text


def test_a_missing_gh_on_post_as_me_is_installed_by_the_agent_account_tool():
    from tools.github_agent_account import ACTIONS
    from tools.managed_runtime import COMMAND_RUNTIME

    text = (SKILLS / "github/references/agent-account.md").read_text(encoding="utf-8")
    assert "- **`gh` missing:** call `github_agent_account` with `action: \"install\"`" in text
    assert "install" in ACTIONS and COMMAND_RUNTIME["gh"] == "gh"
    assert "with `gh` written as the full path `command -v gh` prints in your shell" in text


def test_google_connect_reads_the_downloads_client_file_without_host_access(tmp_path, monkeypatch):
    """The skill passes ~/Downloads/client_secret_<id>.json as is; google_connect reads it itself."""
    import json

    from connections.google.client import read_client_file

    monkeypatch.setenv("HOME", str(tmp_path))
    downloads = tmp_path / "Downloads"
    downloads.mkdir()
    client_id = "123-abc.apps.googleusercontent.com"
    (downloads / f"client_secret_{client_id}.json").write_text(
        json.dumps({"installed": {"client_id": client_id, "client_secret": "fixture-value"}}), encoding="utf-8"
    )

    assert read_client_file(f"~/Downloads/client_secret_{client_id}.json") == (client_id, "fixture-value")
    steps = SKILL.read_text(encoding="utf-8")
    assert "`client_file` set to that path as is" in steps
    assert "never list or check Downloads first and never request host access for it" in steps
