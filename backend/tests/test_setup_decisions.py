"""The setup decisions CORSIA builds for a new install's first run.

Exact strings for every presence combination, the settlements CORSIA makes when
no decision applies, the no-leak rule for MCP servers, and the plan_decision
bounds every payload must satisfy. The expected strings are written out here,
apart from the module that builds them.
"""

from __future__ import annotations

import asyncio
import itertools
from unittest.mock import AsyncMock

import pytest

from onboarding import setup_decisions as setup
from plans import decision_fields

REAL_SAVED_EMAIL = setup._saved_email
GITHUB_TASK = "Set up GitHub"
PROJECT_TASK = "Set up your first agent or project"
WEB = "Connect web search"
OMLX_TASK = "Configuration checks"

DIFFERENCE = ({"setting": "max_concurrent_requests", "current": 4, "recommended": 1},)
SESSIONS = ("Claude Code", "Codex")
GITHUB_OPTIONS = [
    {"id": "A", "label": "Their own account",
     "detail": "I open the browser and fill in the sign-up. You type the password and any code."},
    {"id": "B", "label": "Post as me",
     "detail": "I sign the GitHub CLI in to your account. You approve that sign-in in your browser."},
    {"id": "C", "label": "Not now", "detail": "Ask me any time."},
]


def _github(sign_up_with: str) -> dict:
    return {
        "task_title": GITHUB_TASK,
        "title": "How should your agents post on GitHub?",
        "context": (
            "Agents use GitHub to file issues and feature requests, comment, and send pull "
            "requests: on your own repositories, and on CORSIA's when it falls short and I can "
            "fix it. They can post as you, using your GitHub sign-in, or from their own account, "
            "so their posts stay separate from yours. GitHub allows each person one extra "
            "machine account for this."
        ),
        "tradeoff": (
            "Posting as you takes one sign-in, and everything agents post shows your name. Their "
            "own account takes a few minutes in a browser you can watch: I fill in the sign-up "
            f"with {sign_up_with}, and you type the password and any verification code."
        ),
        "recommendation": "A",
        "rationale": "You can always tell what you wrote from what an agent wrote.",
        "options": GITHUB_OPTIONS,
        "other_enabled": True,
    }


EXPECTED_GITHUB = _github("you+agents@gmail.com")
EXPECTED_GITHUB_NO_ADDRESS = _github("the address you give me in chat")
EXPECTED_FIND_PROJECTS = {
    "task_title": PROJECT_TASK,
    "title": "Look for the projects you already work on?",
    "context": (
        "I can find the projects you work on in Claude Code and Codex. I read only the folder "
        "each session started in (the start of each session file), never your conversations, "
        "and propose which folders belong together as one project. You approve each project "
        "before I create it. That folder list goes to the model I run on. Nothing changes in "
        "Claude Code and Codex."
    ),
    "tradeoff": (
        "Your folder names go to the model I run on. Describing the project yourself sends "
        "nothing, and takes a sentence or a folder path from you."
    ),
    "recommendation": "A",
    "rationale": "It starts from the work you already do, and nothing is created until you approve it.",
    "options": [
        {"id": "A", "label": "Find my projects",
         "detail": "I'll propose your projects for you to approve."},
        {"id": "B", "label": "I'll describe it",
         "detail": "Tell me the folder or a sentence about the work."},
    ],
    "other_enabled": True,
}
EXPECTED_OMLX = {
    "task_title": OMLX_TASK,
    "title": "Change one oMLX setting for CORSIA?",
    "context": (
        "Your oMLX runs the local models on this Mac. It's set to work on 4 requests at once. "
        "At 1, oMLX finishes one request before it starts the next, so each one gets the model's "
        "full speed and memory instead of sharing them. I'd change only this one setting."
    ),
    "tradeoff": (
        "Requests that arrive together, from CORSIA's agents or from other apps that use your "
        "oMLX, will wait their turn instead of running alongside each other. You can change it "
        "back in oMLX's settings at any time."
    ),
    "recommendation": "A",
    "rationale": (
        "It matches how CORSIA sets up an oMLX it installs. Choose B if other apps use your oMLX "
        "all day."
    ),
    "options": [
        {"id": "A", "label": "Change it to 1",
         "detail": "I'll apply it now and tell you if oMLX needs a restart."},
        {"id": "B", "label": "Leave it at 4",
         "detail": "CORSIA still works; requests that arrive together share the model."},
    ],
    "other_enabled": True,
}

# omlx state -> (facts value, whether its decision is present)
OMLX_STATES = {
    "difference": (DIFFERENCE, True),
    "matches": ((), False),
    "none": (None, False),
}
COMBINATIONS = list(itertools.product((True, False), (True, False), tuple(OMLX_STATES)))


def _facts(web_missing: bool, omlx: str, *, self_check: bool = True,
           sessions: bool = False, email: str = "you@gmail.com") -> setup.Facts:
    return setup.Facts(OMLX_STATES[omlx][0], not web_missing, self_check,
                       SESSIONS if sessions else (), email)


@pytest.mark.parametrize("sessions,web_missing,omlx", COMBINATIONS)
def test_exact_decisions_for_every_presence_combination(sessions, web_missing, omlx):
    plan = setup.build_plan(_facts(web_missing, omlx, sessions=sessions))

    expected = [*([EXPECTED_OMLX] if OMLX_STATES[omlx][1] else []),
                EXPECTED_GITHUB,
                *([EXPECTED_FIND_PROJECTS] if sessions else [])]
    assert plan["setup_decisions"] == expected


@pytest.mark.parametrize("sessions,web_missing,omlx", COMBINATIONS)
def test_only_web_search_ever_settles_and_only_when_it_works(sessions, web_missing, omlx):
    settlements = setup.build_plan(_facts(web_missing, omlx, sessions=sessions))["setup_task_settlements"]

    assert settlements == ([] if web_missing else [
        {"task_title": WEB, "outcome": "done", "text": "Web search already works."}])


@pytest.mark.parametrize("sessions,web_missing,omlx", COMBINATIONS)
def test_every_payload_passes_plan_decision_validation(sessions, web_missing, omlx):
    for payload in setup.build_decisions(_facts(web_missing, omlx, sessions=sessions)):
        decision_fields.content({key: value for key, value in payload.items() if key != "task_title"})


def test_a_failed_self_check_leaves_out_only_the_omlx_decision():
    plan = setup.build_plan(_facts(True, "difference", self_check=False, sessions=True))

    assert plan["setup_decisions"] == [EXPECTED_GITHUB, EXPECTED_FIND_PROJECTS]


def test_each_decision_is_raised_from_its_own_task_in_plan_order():
    decisions = setup.build_decisions(_facts(True, "difference", sessions=True))

    assert [d["task_title"] for d in decisions] == [OMLX_TASK, GITHUB_TASK, PROJECT_TASK]
    assert all("pick" not in d for d in decisions)


@pytest.mark.parametrize("email,expected", [
    ("you@gmail.com", "you+agents@gmail.com"),
    ("You.Name@GMAIL.com", "You.Name+agents@gmail.com"),
    ("you+shop@googlemail.com", "you+agents@googlemail.com"),
    ("you@example.com", ""),
    ("you@icloud.com", ""),
    ("@gmail.com", ""),
    ("not an email", ""),
    ("", ""),
])
def test_the_agents_address_adds_plus_agents_for_gmail_only(email, expected):
    assert setup.agents_email(email) == expected


@pytest.mark.parametrize("email", ["you@example.com", ""])
def test_github_never_invents_an_address_for_a_non_gmail_email(email):
    github = next(d for d in setup.build_decisions(_facts(True, "none", email=email))
                  if d["task_title"] == GITHUB_TASK)

    assert github == EXPECTED_GITHUB_NO_ADDRESS
    assert "@" not in github["tradeoff"]


def test_only_the_recommended_oomlx_setting_is_offered():
    other = ({"setting": "something_else", "current": 1, "recommended": 2},)

    assert [d["task_title"] for d in setup.build_decisions(setup.Facts(other, True))] == [GITHUB_TASK]


def test_the_consent_wording_is_the_find_projects_decision():
    find = EXPECTED_FIND_PROJECTS

    assert setup.consent_wordings(SESSIONS) == [(find["title"], find["context"], "Find my projects")]
    assert setup.consent_wordings(()) == []


def test_no_dropped_decision_is_left_in_the_strings():
    from onboarding import setup_strings

    for name in ("MCP_ONLY", "TOOLS_AND_PROJECTS", "FIRST_STEP", "WEB_ONLY", "REVIEWER", "PHONE"):
        assert not hasattr(setup_strings, name), name


@pytest.fixture(autouse=True)
def session_folders(monkeypatch):
    """No test stats the real home folder; each sets the apps it needs."""
    found: list[str] = []
    monkeypatch.setattr("onboarding.project_discovery.session_apps", lambda _roots: list(found))
    return found


@pytest.fixture(autouse=True)
def saved_email(monkeypatch):
    """No test reads a real profile; the install's email is what each sets."""
    monkeypatch.setattr(setup, "_saved_email", lambda: "you@gmail.com")


def test_read_facts_leaves_out_oomlx_when_the_self_check_failed(monkeypatch, session_folders):
    monkeypatch.setattr(setup, "web_search_works", AsyncMock(return_value=False))
    session_folders.append("Codex")

    facts = asyncio.run(setup.read_facts(self_check_passed=False))

    assert facts == setup.Facts(None, False, False, ("Codex",), "you@gmail.com")


def test_read_facts_takes_omlx_from_its_source_and_the_email_from_the_profile(monkeypatch):
    monkeypatch.setattr(setup, "web_search_works", AsyncMock(return_value=True))
    monkeypatch.setattr("api.provider_sources.list_sources", lambda: [
        {"source": "claude-code", "omlx_settings": {"differences": [{"setting": "x"}]}},
        {"source": "omlx", "omlx_settings": {"differences": list(DIFFERENCE), "fingerprint": "f"}},
    ])

    assert asyncio.run(setup.read_facts()) == setup.Facts(DIFFERENCE, True, True, (), "you@gmail.com")


@pytest.mark.parametrize("settings,expected", [(None, None), ([], ())])
def test_unknown_and_matching_omlx_settings_read_differently(monkeypatch, settings, expected):
    monkeypatch.setattr(setup, "web_search_works", AsyncMock(return_value=True))
    monkeypatch.setattr("api.provider_sources.list_sources", lambda: [
        {"source": "omlx", "omlx_settings": settings},
    ])

    assert asyncio.run(setup.read_facts()).omlx_differences == expected


def test_the_saved_email_is_read_from_the_workspace_profile(tmp_path, monkeypatch):
    from infra import config, user_profile

    monkeypatch.setattr(config, "WORKSPACE", tmp_path)
    assert REAL_SAVED_EMAIL() == ""
    assert user_profile.write_user_profile(tmp_path, "Pat", "America/Los_Angeles", "pat@gmail.com")
    assert REAL_SAVED_EMAIL() == "pat@gmail.com"


def test_web_search_works_needs_a_key_and_a_works_outcome(monkeypatch):
    from tools.web_search_outcome import FAILED, KEY_REFUSED, WORKS, SearchOutcome

    seen: list[str] = []
    outcome = SearchOutcome(WORKS)

    async def search(key, query, count):
        seen.append(key)
        return outcome

    monkeypatch.setattr("tools.web_search_outcome.run_search", search)
    monkeypatch.setattr("infra.key_store.resolve_builtin", lambda builtin: None)
    assert asyncio.run(setup.web_search_works()) is False
    assert not seen

    monkeypatch.setattr("infra.key_store.resolve_builtin", lambda builtin: ("k", "store"))
    for outcome, works in ((SearchOutcome(WORKS), True), (SearchOutcome(KEY_REFUSED), False),
                           (SearchOutcome(FAILED, "network"), False)):
        assert asyncio.run(setup.web_search_works()) is works
    assert seen == ["k", "k", "k"]



def test_one_key_resolver_serves_the_probe_and_web_search(isolated_db, tmp_path, monkeypatch):
    import tools.web as web
    from infra import key_store
    from tools.web_search_outcome import WORKS, SearchOutcome

    monkeypatch.setattr("infra.config.STATE_ROOT", tmp_path)
    monkeypatch.delenv("BRAVE_API_KEY", raising=False)
    seen: list[str] = []

    async def search(key, query, count):
        seen.append(key)
        return SearchOutcome(WORKS)

    monkeypatch.setattr("tools.web_search_outcome.run_search", search)
    monkeypatch.setattr(web, "run_search", search)
    key_store.set_value("web-search", "from-store")
    assert asyncio.run(setup.web_search_works()) is True
    asyncio.run(web.WebSearchTool().execute("q"))
    monkeypatch.setenv("BRAVE_API_KEY", "from-env")
    key_store.clear_value("web-search")
    assert asyncio.run(setup.web_search_works()) is False
    asyncio.run(web.WebSearchTool().execute("q"))

    assert seen == ["from-store", "from-store"]
    assert not hasattr(web, "resolve_search_key")
    assert not hasattr(setup, "_web_search_key")
