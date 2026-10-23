"""The setup decisions CORSIA builds for a new install's first run.

Exact strings for every presence combination, the settlements CORSIA makes when
no decision applies, the no-leak rule for MCP servers, and the plan_decision
bounds every payload must satisfy. The expected strings are written out here,
apart from the module that builds them.
"""

from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock

import pytest

from onboarding import setup_decisions as setup
from plans import decision_fields

REAL_SAVED_EMAIL = setup._saved_email
GITHUB_TASK = "Set up GitHub"
PROJECT_TASK = "Set up your first project"
WEB = "Connect web search"

SESSIONS = ("Claude Code", "Codex")
GITHUB_OPTIONS = [
    {"id": "A", "label": "Their own account",
     "detail": "I open the browser and fill in the sign-up. You type the password and any code."},
    {"id": "B", "label": "Use mine",
     "detail": ("Agents use the GitHub sign-in already on this computer, or I sign the GitHub CLI in "
                "to your account and you approve that sign-in in your browser.")},
    {"id": "C", "label": "Not now", "detail": "Ask me any time."},
]
GITHUB_NO_ACCOUNT_OPTIONS = [
    {"id": "A", "label": "Make their account",
     "detail": "I open the browser and fill in the sign-up. You type the password and any code."},
    {"id": "B", "label": "Not now", "detail": "Ask me any time."},
]
EXPECTED_COMMUNITY = {
    "task_title": GITHUB_TASK,
    "title": "Use and share community improvements?",
    "context": ("You can always turn down a change and keep your own version, and nothing private ever goes "
                "into what's shared."),
    "tradeoff": "Sharing goes through GitHub, so choosing Use and share leads to one question about your account.",
    "recommendation": "A",
    "rationale": "You get what others prove, and a proven change helps everyone using CORSIA.",
    "options": [
        {"id": "A", "label": "Use and share",
         "detail": "Agents take improvements other people proved and offer the ones proven here."},
        {"id": "B", "label": "Only use", "detail": "Agents take proven improvements and share nothing."},
        {"id": "C", "label": "Neither", "detail": "Agents keep their improvements here."},
    ],
    "other_enabled": True,
}


def _github(sign_up_with: str) -> dict:
    return {
        "task_title": GITHUB_TASK,
        "title": "How should your agents post on GitHub?",
        "context": (
            "Agents use GitHub to report problems and send fixes to CORSIA, and to work on "
            "your repositories. GitHub allows each person one extra machine account for this. "
            "Reports to CORSIA are text only and go only with your OK, from whichever you choose."
        ),
        "tradeoff": (
            "Their own account takes a few minutes in a browser you can watch: I fill in the "
            f"sign-up with {sign_up_with}, and you type the password and any verification code. "
            "Using yours takes one sign-in, and everything agents post shows your name."
        ),
        "recommendation": "A",
        "rationale": "You can always tell what you wrote from what an agent wrote.",
        "options": GITHUB_OPTIONS,
        "other_enabled": True,
    }


def _github_no_account(sign_up_with: str) -> dict:
    return {
        "task_title": GITHUB_TASK,
        "title": "Make an account for your agents on GitHub?",
        "context": (
            "Agents use GitHub to report problems and send fixes to CORSIA, and to work on "
            "your repositories. GitHub allows each person one extra machine account for this."
        ),
        "tradeoff": (
            "It takes a few minutes in a browser you can watch: I fill in the sign-up with "
            f"{sign_up_with}, and you type the password and any verification code."
        ),
        "recommendation": "A",
        "rationale": "You can always tell what you wrote from what an agent wrote.",
        "options": GITHUB_NO_ACCOUNT_OPTIONS,
        "other_enabled": True,
    }


EXPECTED_GITHUB = _github("you+agents@gmail.com")
EXPECTED_GITHUB_NO_ACCOUNT = _github_no_account("you+agents@gmail.com")
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
COMBINATIONS = [(sessions, web_missing) for sessions in (True, False) for web_missing in (True, False)]


def _facts(web_missing: bool, *, self_check: bool = True,
           sessions: bool = False, email: str = "you@gmail.com") -> setup.Facts:
    return setup.Facts(not web_missing, self_check, SESSIONS if sessions else (), email)


@pytest.mark.parametrize("sessions,web_missing", COMBINATIONS)
def test_exact_decisions_for_every_presence_combination(sessions, web_missing):
    plan = setup.build_plan(_facts(web_missing, sessions=sessions))

    expected = [EXPECTED_COMMUNITY, EXPECTED_GITHUB, EXPECTED_GITHUB_NO_ACCOUNT, *([EXPECTED_FIND_PROJECTS] if sessions else [])]
    assert plan["setup_decisions"] == expected


@pytest.mark.parametrize("sessions,web_missing", COMBINATIONS)
def test_only_web_search_ever_settles_and_only_when_it_works(sessions, web_missing):
    settlements = setup.build_plan(_facts(web_missing, sessions=sessions))["setup_task_settlements"]

    assert settlements == ([] if web_missing else [
        {"task_title": WEB, "outcome": "done", "text": "Web search already works."}])


@pytest.mark.parametrize("sessions,web_missing", COMBINATIONS)
def test_every_payload_passes_plan_decision_validation(sessions, web_missing):
    for payload in setup.build_decisions(_facts(web_missing, sessions=sessions)):
        decision_fields.content({key: value for key, value in payload.items() if key != "task_title"})



def test_each_decision_is_raised_from_its_own_task_in_plan_order():
    decisions = setup.build_decisions(_facts(True, sessions=True))

    assert [d["task_title"] for d in decisions] == [GITHUB_TASK] * 3 + [PROJECT_TASK]
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
    github = next(d for d in setup.build_decisions(_facts(True, email=email))
                  if d["title"] == EXPECTED_GITHUB["title"])

    assert github == EXPECTED_GITHUB_NO_ADDRESS
    assert "@" not in github["tradeoff"]


def test_the_community_decision_comes_first_and_the_github_cards_drop_the_reasons():
    decisions = [d for d in setup.build_decisions(_facts(True)) if d["task_title"] == GITHUB_TASK]

    assert [d["title"] for d in decisions] == [
        "Use and share community improvements?",
        "How should your agents post on GitHub?", "Make an account for your agents on GitHub?"]
    assert [o["label"] for o in decisions[0]["options"]] == ["Use and share", "Only use", "Neither"]
    assert decisions[0]["recommendation"] == "A"
    assert "Reports to CORSIA are text only" in decisions[1]["context"]
    text = " ".join(str(v) for d in decisions for v in d.values())
    assert "AI" not in text



def test_the_consent_wording_is_the_find_projects_decision():
    find = EXPECTED_FIND_PROJECTS

    assert setup.consent_wordings(SESSIONS) == [(find["title"], find["context"], "Find my projects")]
    assert setup.consent_wordings(()) == []


def test_no_dropped_decision_is_left_in_the_strings():
    from onboarding import setup_strings

    for name in ("MCP_ONLY", "TOOLS_AND_PROJECTS", "FIRST_STEP", "WEB_ONLY", "REVIEWER", "PHONE",
                 "OMLX", "OMLX_MANAGED", "OMLX_GROUP_GLOBAL", "OMLX_GROUP_MODEL", "OMLX_ROW",
                 "OMLX_RESTORE", "OMLX_RESTORE_MODEL", "OMLX_LABELS", "OMLX_DEFAULT", "omlx_value"):
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



def test_read_facts_reads_email_and_skips_provider_sources_without_an_agent_model(monkeypatch):
    monkeypatch.setattr(setup, "web_search_works", AsyncMock(return_value=True))
    monkeypatch.setattr("api.provider_sources.list_sources", lambda: pytest.fail("provider source read"))

    assert asyncio.run(setup.read_facts()) == setup.Facts(True, True, (), "you@gmail.com")


def test_the_saved_email_is_read_from_the_workspace_profile(tmp_path, monkeypatch):
    from infra import config, user_profile

    monkeypatch.setattr(config, "WORKSPACE", tmp_path)
    assert REAL_SAVED_EMAIL() == ""
    path = tmp_path / user_profile.PROFILE_RELATIVE_PATH
    path.parent.mkdir(parents=True)
    path.write_text(user_profile.profile_text("Pat", "America/Los_Angeles", "pat@gmail.com"), encoding="utf-8")
    assert REAL_SAVED_EMAIL() == "pat@gmail.com"


def test_web_search_works_runs_a_real_keyless_search_before_settling_setup(monkeypatch):
    from tools.web_omlx_search import NO_SERVER, SERVER_STOPPED
    from tools.web_search_outcome import CHECK_QUERY, FAILED, KEY_REFUSED, WORKS, SearchOutcome

    seen: list[str] = []
    outcome = SearchOutcome(WORKS)

    async def search(key, query, count):
        seen.append(key)
        return outcome

    monkeypatch.setattr("tools.web_search_outcome.run_search", search)
    monkeypatch.setattr("infra.key_store.resolve_builtin", lambda builtin: None)
    hit = SearchOutcome(WORKS, results=[{"title": "CORSIA", "url": "https://corsia.test"}])
    for keyless_result, works in ((hit, True), (SearchOutcome(WORKS), False),
                                  (SearchOutcome(FAILED, "provider"), False),
                                  (NO_SERVER, False), (SERVER_STOPPED, False)):
        keyless = AsyncMock(return_value=keyless_result)
        monkeypatch.setattr("tools.web_omlx_search.keyless_search", keyless)
        assert asyncio.run(setup.web_search_works()) is works
        keyless.assert_awaited_once_with(CHECK_QUERY)
    assert setup.build_settlements(setup.Facts(web_search_works=True)) == [{
        "task_title": WEB, "outcome": "done", "text": "Web search already works.",
    }]
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
