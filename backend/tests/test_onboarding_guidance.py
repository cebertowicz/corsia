from __future__ import annotations

import re
from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]
BOOTSTRAP_SKILL = ROOT / "backend/prompts/system/v1/skills/conductor-bootstrap/SKILL.md"
FIRST_RUN = BOOTSTRAP_SKILL.parent / "references/first-run.md"
OBSERVABILITY = ROOT / "docs/observability.md"


def test_bootstrap_discloses_missing_published_models_before_drafting_from_scratch():
    skill = BOOTSTRAP_SKILL.read_text(encoding="utf-8")

    assert "published models could not be read" in skill
    assert "proposal is from scratch and may miss published guidance" in skill
    assert "Do not claim a published model was loaded." in skill


def test_first_run_explains_model_capacity_before_asking_for_a_subscription():
    first_run = FIRST_RUN.read_text(encoding="utf-8")
    provider_task = first_run.split("## Task 2:", 1)[1].split("## Task 3:", 1)[0]

    assert "one of those should carry the daily work" in provider_task
    assert "it can run a few agents, not the team" in provider_task
    assert "Tell me which, or say not now." in provider_task
    assert "A subscription (Claude Code or Codex)" not in first_run


def test_observability_documents_all_content_free_onboarding_emit_points():
    observability = OBSERVABILITY.read_text(encoding="utf-8")
    taxonomy = observability.split("### Messages", maxsplit=1)[0]

    assert taxonomy.count("| `onboarding.") == 6
    for event in (
        "onboarding.account_created",
        "onboarding.first_run_wake",
        "onboarding.first_model_selected",
        "onboarding.first_turn_completed",
        "onboarding.first_decision_answered",
        "onboarding.first_outcome",
    ):
        assert f"`{event}`" in taxonomy

    assert "They contain no user, project, thread, or model identifiers, decision text, or message content" in observability
    assert "local and non-load-bearing" in observability
    assert "`source_kind` (the model dispatch prefix only)" in observability
    assert (
        "`option` (the selected option identifier, truncated to 8 characters; for a pick-any "
        "decision, the chosen ids joined by commas, so up to four fit)"
    ) in observability
    assert "`operation` ∈ `mark_done \\| accept`" in observability


REMOTE_ACCESS = BOOTSTRAP_SKILL.parent / "references/remote-access.md"
WEB_SEARCH = BOOTSTRAP_SKILL.parent / "references/web-search.md"
PROJECT_IMPORT = BOOTSTRAP_SKILL.parent / "references/project-import.md"


def test_first_run_hands_phone_access_to_the_remote_access_reference():
    first_run = FIRST_RUN.read_text(encoding="utf-8")
    remote = REMOTE_ACCESS.read_text(encoding="utf-8")

    assert "`references/remote-access.md`" in first_run
    assert "references/remote-access.md" in BOOTSTRAP_SKILL.read_text(encoding="utf-8")
    assert "tailscale serve --bg --https=[https port] http://127.0.0.1:[port]" in remote
    assert "never by its https port" in remote
    assert "Never present an unconfirmed value as fact." in remote
    assert "report the actual output" in remote


def test_settings_keys_is_spelled_by_the_one_constant():
    from infra.ui_labels import SETTINGS_KEYS

    for path in (FIRST_RUN, REMOTE_ACCESS, WEB_SEARCH):
        text = path.read_text(encoding="utf-8")
        for spelling in re.findall(r"Settings\s*[^\w\s`\[\]]\s*Keys", text):
            assert spelling == SETTINGS_KEYS


def test_first_run_sets_the_reviewer_in_chat_and_never_enables_auto_approve():
    first_run = FIRST_RUN.read_text(encoding="utf-8")
    task_3 = first_run.split("## Task 3:", 1)[1].split("## Task 4:", 1)[0]

    assert 'path="/api/settings", body={"host_access_reviewer_model":' in task_3
    assert "only on status 200, post" in task_3
    assert "host_access_reviewer_status" not in task_3
    assert "security and cost choice" in task_3
    assert "Never enable it yourself or send its setting through `corsia_api`." in task_3
    assert "host_access_auto_approve" not in first_run
    assert "mistake" not in task_3.lower()
    assert "`Checked: " in task_3
    assert "Pick the host access reviewer model" not in first_run


SKILL_TEXT = BOOTSTRAP_SKILL.read_text(encoding="utf-8")
TITLES = (
    "Choose a folder or describe your work",
    "Pick a model for [project]",
    "Add your web search key",
    "Add a key Brave accepts",
    "Install Tailscale on this Mac",
    "Say try again for phone access",
)


def test_a_wait_on_the_person_is_a_user_task_not_a_description_line():
    first_run = FIRST_RUN.read_text(encoding="utf-8")
    remote = REMOTE_ACCESS.read_text(encoding="utf-8")
    web_search = WEB_SEARCH.read_text(encoding="utf-8")
    project_import = PROJECT_IMPORT.read_text(encoding="utf-8")

    for text in (first_run, remote, web_search, project_import, SKILL_TEXT):
        assert "Waiting on you:" not in text
        assert "Starts after" not in text
    assert "assignee='user'" in SKILL_TEXT and "`add_dependency`" in SKILL_TEXT
    for title in TITLES:
        assert f"`{title}`" in first_run + remote + web_search + project_import
        assert len(title) < 60


def test_the_skill_marks_a_person_task_done_when_they_answer_in_chat():
    assert "then `mark_done` their task with evidence in plain words" in SKILL_TEXT
    assert "never `mark_done` a user task" not in SKILL_TEXT
    assert "`references/web-search.md`" in FIRST_RUN.read_text(encoding="utf-8")
    assert "`mark_done` their task with evidence in plain words" in WEB_SEARCH.read_text(encoding="utf-8")
    assert "`mark_done` their task with evidence in plain words" in FIRST_RUN.read_text(encoding="utf-8")
    assert "then closes the old task with its reason" in SKILL_TEXT


def test_each_declined_setup_task_closes_with_where_to_find_it():
    first_run = FIRST_RUN.read_text(encoding="utf-8")

    assert "`You chose not now. Settings \u203a Providers has it.`" in first_run
    assert first_run.count("`You chose not now. Ask me any time.`") >= 3
    assert "Settings \u203a Keys has it." in first_run


def test_every_settings_link_in_first_run_opens_a_real_section_and_anchor():
    first_run = FIRST_RUN.read_text(encoding="utf-8")
    web_search = WEB_SEARCH.read_text(encoding="utf-8")
    navigation = (ROOT / "frontend/src/lib/stores/settingsNavigation.ts").read_text(encoding="utf-8")
    sections = set(re.findall(r"'([a-z]+)'", navigation.split("SETTINGS_SECTIONS = [", 1)[1].split("]", 1)[0]))
    anchors = {
        anchor
        for path in (ROOT / "frontend/src/lib/components").glob("*.svelte")
        for anchor in re.findall(r'data-settings-anchor="([\w-]+)"', path.read_text(encoding="utf-8"))
    }
    links = re.findall(r"\]\(/settings/([a-z]+)(?:#([\w-]+))?\)", first_run + web_search)

    assert {section for section, _ in links} >= {"keys", "system"}
    for section, anchor in links:
        assert section in sections
        assert not anchor or anchor in anchors
    assert "[Brave Search API sign-up](https://api-dashboard.search.brave.com/register)" in web_search
    assert "in-app link" not in first_run + web_search


def test_first_run_stays_within_its_body_size():
    # Its size after the fuller conductor-role intro and the MCP import consent step (#690),
    # rounded up; #674 tracks the real cap.
    assert len(FIRST_RUN.read_bytes()) <= 17600  # +the router to first-run-app.md


LOCAL_MODEL_OFFER = BOOTSTRAP_SKILL.parent / "references/local-model-offer.md"


def test_local_model_offer_reads_a_running_setup_before_offering_and_never_waits_on_it():
    offer = LOCAL_MODEL_OFFER.read_text(encoding="utf-8")
    running = offer.split("## Setup already running", 1)[1]

    assert offer.index("`state` is `running` or `downloading`") < offer.index("2. Ask once")
    assert "make no offer" in offer
    assert "never holds up a setup task" in running
    assert "at the end of each later setup task" in running
    assert "`succeeded`" in running and "`failed`" in running and "Each is said once." in running
    assert "never post `result.next_action`" in running
    assert "re-read it after each later task" in FIRST_RUN.read_text(encoding="utf-8")


FIRST_RUN_APP = BOOTSTRAP_SKILL.parent / "references/first-run-app.md"
HANDOFF = "That's everything. So, what's taking up your time this week?"


def test_first_run_routes_the_app_path_to_its_own_reference():
    assert "`first_run_path`" in FIRST_RUN.read_text(encoding="utf-8")
    assert "references/first-run-app.md" in FIRST_RUN.read_text(encoding="utf-8")


def test_app_path_sends_the_message_then_seven_steps_and_ends_on_the_handoff():
    app = FIRST_RUN_APP.read_text(encoding="utf-8")
    steps = app.split("## The steps, in this order", 1)[1].split("## The hand-off", 1)[0]
    titles = re.findall(r"^\d\. \*\*([^*]+)\*\*", steps, flags=re.MULTILINE)

    assert app.index("I'm CORSIA, your assistant.") < app.index("## The steps")
    assert len(titles) == 7
    assert titles[0] == "About you."
    assert app.split("## The hand-off", 1)[1].strip().startswith("After the last step that applies, post exactly `" + HANDOFF + "`")
    assert "never skill `assistant-onboarding`" in steps
    assert "no setup task list" in app
    assert "Read nothing until a yes" in steps


def test_app_path_reads_no_other_app_settings_before_a_yes():
    app = FIRST_RUN_APP.read_text(encoding="utf-8")
    assert "mcp_sources" not in app
    assert "none of another app's settings" in app
