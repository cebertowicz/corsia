"""``agent_import``: the consent card, who may call it, and one read per consent (IMP-13, G6)."""

from __future__ import annotations

import asyncio
import datetime
import json
from types import SimpleNamespace

import pytest

from onboarding import setup_decisions as setup
from onboarding.setup_decisions import ImportCard
from plans import service
from plans.boundary import PlanActor
from tests.test_agent_import_skills import _skill
from tests.test_plan_decisions import _mid, _task, db  # noqa: F401 - shared fixtures
from tools import agent_import as tool_module

CARD = ImportCard(("Claude Code", "Codex"), ("Claude Code", "Codex"), "Claude, by Anthropic")


def _fields(card=CARD):
    fields = setup.agent_import(card)
    fields.pop("task_title")
    return fields


def test_the_card_is_appendix_a1_word_for_word():
    card = setup.agent_import(CARD)
    assert card["title"] == "Bring over what your other AI tools know"
    assert card["context"] == (
        "Claude Code and Codex are on this Mac. I can read what they remember about you and how "
        "you like to work, the skills you keep in them, and the connected apps that "
        "Claude Code and Codex use, so you don't have to set them up again. I only read: nothing "
        "changes in them, and you choose what I keep. I never read your chats with them, their "
        "other settings, or any key or password. What I read goes to Claude, by Anthropic, the model I run on."
    )
    assert card["tradeoff"].startswith("It takes about a minute.")
    assert [(o["id"], o["label"]) for o in card["options"]] == [("A", "Read them"), ("B", "Not now")]
    assert card["recommendation"] == "A"


def test_openclaw_alone_promises_no_connections_and_a_local_model_keeps_it_here():
    context = setup.agent_import(ImportCard(("OpenClaw",), (), ""))["context"]
    assert context.startswith("OpenClaw is on this Mac. ")
    assert "connected apps" not in context
    # With no connections clause nothing is read from settings, so none are excepted.
    assert "with them, their settings, or any key" in context
    assert context.endswith("What I read stays on this Mac: I run on a local model.")
    one = setup.agent_import(ImportCard(("Codex", "Hermes Agent"), ("Codex",), "GPT, by OpenAI"))["context"]
    assert "the connected apps that Codex uses," in one
    assert "their other settings" in one


def test_a_model_with_no_known_provider_says_the_read_may_leave_this_mac():
    context = setup.agent_import(ImportCard(("Codex",), (), "Some Model"))["context"]
    assert context.endswith("What I read goes to Some Model, the model I run on, and may leave this Mac.")


def test_no_tool_found_means_no_card_and_the_card_goes_first(monkeypatch):
    assert setup.agent_import(None) is None
    decisions = setup.build_decisions(setup.Facts(agent_import=CARD))
    assert decisions[0]["title"] == "Bring over what your other AI tools know"
    assert setup.build_decisions(setup.Facts())[0]["title"] != decisions[0]["title"]


@pytest.mark.parametrize(("model", "target", "expected"), [
    ("omlx@s/q", SimpleNamespace(provider="omlx", harness="", display_name="Local"), ""),
    ("claude-code/opus", SimpleNamespace(provider="anthropic", harness="claude-code", display_name="Opus"),
     "Claude, by Anthropic"),
    ("codex/terra", SimpleNamespace(provider="openai", harness="codex", display_name="Terra"), "GPT, by OpenAI"),
    # An External endpoint may front a cloud API, so it is never read as local.
    ("external@s/m", SimpleNamespace(provider="external", harness="", display_name="Some Model"), "Some Model"),
])
def test_the_model_sentence_names_who_receives_the_read(monkeypatch, model, target, expected):
    from llm import resolution

    monkeypatch.setattr(resolution, "resolve_execution_target", lambda _model: target)
    assert setup.model_provider(model) == expected


def test_an_unresolvable_model_means_no_card(monkeypatch, tmp_path):
    from llm import resolution

    def unavailable(_model):
        raise resolution.ModelUnavailableError("m", "gone")

    (tmp_path / ".codex" / "AGENTS.md").parent.mkdir(parents=True)
    (tmp_path / ".codex" / "AGENTS.md").write_text("x")
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setattr(resolution, "resolve_execution_target", unavailable)
    assert setup.import_card("m", {"codex"}) is None


# --- who may call it (G6) ------------------------------------------------------


@pytest.mark.parametrize(("corsia", "first_run", "project", "allowed"), [
    (True, False, None, True),
    (False, True, None, True),
    (False, True, "project-1", False),
    (False, False, None, False),
])
def test_only_the_corsia_agent_and_a_first_run_thread_may_call_it(monkeypatch, corsia, first_run, project, allowed):
    from infra import corsia_agent

    monkeypatch.setattr(corsia_agent, "is_corsia_agent", lambda _tid: corsia)
    monkeypatch.setattr(setup, "has_open_setup_task", lambda _tid: first_run)
    assert tool_module._allowed("t", project) is allowed


def test_a_project_agent_is_refused_before_anything_is_read(monkeypatch):
    monkeypatch.setattr(tool_module, "_allowed", lambda _tid, _project: False)
    monkeypatch.setattr(tool_module, "_card_facts", lambda _tid: pytest.fail("read facts"))
    assert asyncio.run(_execute(project_id="project-1")) == f"❌ {tool_module.NOT_HERE}"


def test_contract_catalog_and_registration():
    from core.agent import _register_project_creation_tools
    from tools.catalog import TOOL_CATALOG

    tool = tool_module.AgentImportTool()
    assert set(tool.parameters["properties"]) == {"consent_decision_id", "mode", "item_ids", "tools"}
    assert "references/agent-import.md" in tool.description
    assert TOOL_CATALOG["agent_import"].category == "memory"
    registered = []
    _register_project_creation_tools(SimpleNamespace(tools=SimpleNamespace(register=registered.append)))
    assert [t.name for t in registered].count("agent_import") == 1


# --- one read per consent --------------------------------------------------------


def _actor(thread_id="owner"):
    return PlanActor(thread_id, "user-1", "project-1")


async def _execute(thread_id="owner", project_id="project-1", **kwargs):
    import tools.delegation as delegation

    original = delegation._current_thread_context
    delegation._current_thread_context = lambda _tid: {"user_id": "user-1", "project_id": project_id}
    try:
        return await tool_module.AgentImportTool().execute(_thread_id=thread_id, **kwargs)
    finally:
        delegation._current_thread_context = original


@pytest.fixture
def wired(db, tmp_path, monkeypatch):  # noqa: F811 - the shared fixture
    """The tool on the test plan database, with fixture homes for Claude Code and Codex.

    The workspace is swapped through the tool's own seam, never ``infra.config.WORKSPACE``:
    modules imported later (``tools.sandbox_config``) snapshot that global for the process.
    """
    from plans import public

    home = tmp_path / "home"
    monkeypatch.setenv("HOME", str(home))
    (home / ".claude").mkdir(parents=True)
    (home / ".claude" / "CLAUDE.md").write_text("Maya Chen prefers short answers")
    _skill(home / ".codex" / "skills" / "rfc-writer", "rfc-writer", "drafts RFCs")
    monkeypatch.setattr(tool_module, "_workspace", lambda: tmp_path / "workspace")
    monkeypatch.setattr(tool_module, "_allowed", lambda _tid, _project: True)
    monkeypatch.setattr(tool_module, "_card_facts", lambda _tid: CARD)

    async def in_plan(job):
        with public.plan_transaction(db):
            return job(db)

    monkeypatch.setattr(tool_module, "_in_plan", in_plan)
    return db


def _consent(db, option_id="A", fields=None):  # noqa: F811
    from plans import decisions

    plan = service.ensure_plan("thread", "owner", db=db)
    source = _task(plan.id, "Getting started", db=db)
    db.commit()
    decision = decisions.draft(plan.id, source_node_id=source.id, mutation_id=_mid(), actor=_actor(),
                               provenance="tool.plans.decision", db=db, **(fields or _fields())).result["decision"]
    for step in (decisions.parent_review, decisions.raise_to_user):
        decision = step(decision["id"], expected_revision=decision["revision"], mutation_id=_mid(),
                        actor=_actor(), provenance="p", db=db).result["decision"]
    decision = decisions.answer(decision["id"], option_id=option_id, other_text=None,
                                expected_revision=decision["revision"], mutation_id=_mid(),
                                actor=PlanActor(None, "user-1", None, kind="user"),
                                provenance="api.plans.decision", db=db).result["decision"]
    db.commit()
    return decision["id"]


def _receipt(db, operation):
    row = db.execute("SELECT result_json FROM plan_mutation_receipts WHERE operation=?", (operation,)).fetchone()
    return json.loads(row["result_json"]) if row else None


def test_no_id_returns_the_card_to_raise_and_tools_narrow_it(wired):
    payload = json.loads(asyncio.run(_execute()))
    assert payload["consent_decision"] == _fields()
    assert "raise the import consent decision first" in payload["error"]
    narrowed = json.loads(asyncio.run(_execute(tools=["codex"])))["consent_decision"]
    assert narrowed == _fields(ImportCard(("Codex",), ("Codex",), CARD.provider))
    # A named tool that isn't here is reported; it never widens the card to every found tool.
    partly = json.loads(asyncio.run(_execute(tools=["codex", "openclaw"])))
    assert partly["consent_decision"] == narrowed
    assert partly["not_found"] == ["OpenClaw"]
    none_here = json.loads(asyncio.run(_execute(tools=["hermes"])))
    assert "consent_decision" not in none_here
    assert none_here["error"].startswith("none of the tools the person named were found on this Mac (found: Claude Code, Codex)")


def test_answer_a_allows_one_read_with_a_content_free_receipt(wired):
    decision_id = _consent(wired)
    result = json.loads(asyncio.run(_execute(consent_decision_id=decision_id)))
    assert result["result"] == "read"
    assert "Maya Chen" in result["excerpts"][0]["text"]
    assert [row["name"] for row in result["skills"]] == ["rfc-writer"]
    assert result["skipped_files"] == []
    consumed = _receipt(wired, tool_module.CONSENT_OPERATION)
    assert set(consumed) == {"consumed", "read_at"}
    read = _receipt(wired, tool_module.READ_OPERATION)
    assert read["tools"] == ["claude-code", "codex"]
    assert read["opened"] == [{"path": "~/.claude/CLAUDE.md", "bytes": 31}]
    assert read["offers"] == [result["skills"][0]["item_id"]]
    assert "Maya" not in json.dumps(read)
    again = asyncio.run(_execute(consent_decision_id=decision_id))
    assert again.startswith("❌") and "already used" in again


def test_read_reports_skipped_display_names_without_their_content(wired, monkeypatch):
    from onboarding import agent_import

    monkeypatch.setattr(
        agent_import,
        "read_memory",
        lambda *_args, **_kwargs: agent_import.MemoryRead(
            skipped=[{"path": "~/.claude/CLAUDE.md", "reason": "couldn't read: Permission denied"}], failed=True
        ),
    )
    result = json.loads(asyncio.run(_execute(consent_decision_id=_consent(wired))))

    assert result["result"] == "read"
    assert result["skipped_files"] == ["CLAUDE.md"]
    assert "Permission denied" not in json.dumps(result)


def test_import_events_expose_only_counts_and_outcomes(wired, monkeypatch):
    events = []
    monkeypatch.setattr(tool_module, "_emit_event", lambda _thread, event, **properties: events.append((event, properties)))

    assert "consent_decision" in json.loads(asyncio.run(_execute()))
    decision_id = _consent(wired)
    item_id = json.loads(asyncio.run(_execute(consent_decision_id=decision_id)))["skills"][0]["item_id"]
    asyncio.run(_execute(consent_decision_id=decision_id, mode="install", item_ids=[item_id]))

    assert events == [
        ("consent_card", {"tools_count": 2}),
        ("read", {"excerpts_count": 1, "skills_count": 1, "skipped_count": 0}),
        ("install", {"outcome": "installed", "count": 1}),
    ]


def test_installs_from_that_read_for_one_hour_only(wired, monkeypatch):
    decision_id = _consent(wired)
    item_id = json.loads(asyncio.run(_execute(consent_decision_id=decision_id)))["skills"][0]["item_id"]
    stray = asyncio.run(_execute(consent_decision_id=decision_id, mode="install", item_ids=["nope"]))
    assert "were not offered as new by that read: nope" in stray
    done = json.loads(asyncio.run(_execute(consent_decision_id=decision_id, mode="install", item_ids=[item_id])))
    assert done["installed"] == 1
    twice = asyncio.run(_execute(consent_decision_id=decision_id, mode="install", item_ids=[item_id]))
    assert "already installed from that read" in twice
    later = datetime.datetime.now(datetime.timezone.utc) + datetime.timedelta(hours=1, minutes=1)
    monkeypatch.setattr(tool_module, "_now", lambda: later)
    expired = asyncio.run(_execute(consent_decision_id=decision_id, mode="install", item_ids=[item_id]))
    assert "that hour has passed" in expired


def test_install_needs_a_finished_read(wired):
    decision_id = _consent(wired)
    refused = asyncio.run(_execute(consent_decision_id=decision_id, mode="install", item_ids=["x"]))
    assert "has no finished read" in refused


@pytest.mark.parametrize(("kwargs", "message"), [
    ({"thread_id": "worker"}, "not in your plan"),
    ({"option": "B"}, "did not choose to have their other AI tools read"),
    ({"facts": ImportCard(("Codex",), ("Codex",), "Claude, by Anthropic")}, "does not carry the fixed import consent wording"),
])
def test_a_consent_that_cannot_authorize_refuses_and_reads_nothing(wired, monkeypatch, kwargs, message):
    from onboarding import agent_import

    decision_id = _consent(wired, kwargs.get("option", "A"))
    if "facts" in kwargs:
        monkeypatch.setattr(tool_module, "_card_facts", lambda _tid: kwargs["facts"])
    monkeypatch.setattr(agent_import, "read_memory", lambda *a, **k: pytest.fail("read"))
    refused = asyncio.run(_execute(kwargs.get("thread_id", "owner"), consent_decision_id=decision_id))
    assert refused.startswith("❌") and message in refused
    assert _receipt(wired, tool_module.READ_OPERATION) is None


# --- first run -------------------------------------------------------------------


def test_the_app_wake_builds_the_card_for_the_conductors_model(monkeypatch):
    from onboarding import first_run_wake

    seen = {}

    async def read_facts(**kwargs):
        seen.update(kwargs)
        return setup.Facts(agent_import=CARD)

    monkeypatch.setattr(setup, "read_facts", read_facts)
    built = asyncio.run(first_run_wake._app_setup("claude-code/sonnet"))
    assert seen["agent_model"] == "claude-code/sonnet"
    assert built["setup_decisions"][0]["title"] == "Bring over what your other AI tools know"


def test_first_run_raises_the_card_first_and_the_reference_carries_appendix_a():
    from pathlib import Path

    skill = Path(__file__).parents[1] / "prompts/system/v1/skills/conductor-bootstrap"
    app = (skill / "references/first-run-app.md").read_text(encoding="utf-8")
    steps = app.split("## The steps, in this order", 1)
    assert "raise it first" in steps[0] and "`references/agent-import.md`" in steps[0]
    assert "or what the person confirmed earlier in this conversation" in steps[1]
    assert "already said yes for the tools its connections clause named" in steps[1]
    assert "OpenClaw and Hermes Agent included" in steps[1]
    assert 'On "not now" to that shorter question, list only the card\'s tools.' in steps[1]
    guide = (skill / "references/agent-import.md").read_text(encoding="utf-8")
    for line in (
        "Kept [N] things about you and how you work.",
        "Brought over [installed] skills. They’re in Settings › Skills. Tell me if you want any of them removed.",
        "Nothing new to bring over.",
        "I looked, and they don’t hold much about you, so I kept nothing.",
        "Ask me any time to bring things over from them.",
        "I couldn’t read them this time, so I kept nothing. Ask me any time to try again.",
        "a CORSIA skill already has that name. Rename it in [tool] and ask me again.",
        "it has a key or password in it, so I left it. Remove that and ask me again.",
        # C9: every held-back or failed skill says why and what to do next.
        "too big to bring over. Make it smaller in [tool] and ask me again.",
        # C11: an unreadable skill names its one cause; C3: "symbolic link", not "link".
        "`link`: `it has a symbolic link in it. Replace the link with the file itself in [tool]",
        "`not_text`: `it has a file that isn’t text.",
        "`no_skill_md`: `it has no SKILL.md. Add one in [tool] and ask me again.`",
        "`bad_header`: `the header at the top of its SKILL.md has a formatting error.",
        "`bad_name`: `its name can’t be used as a skill name. Start it with a letter or number,",
        "`no_description`: `its SKILL.md has no description. Add one in [tool] and ask me again.`",
        "I couldn’t read it this time. Ask me again to try once more.",
        "is different from the version I showed you. Ask me again to see the current version.",
        "so I left it out. Ask me to try again, or to write a CORSIA skill that does the same.",
        "`sha256`",
    ):
        assert line in guide
    assert "`references/agent-import.md`" in (skill / "SKILL.md").read_text(encoding="utf-8")
