"""``agent_import``: bring over what the person's other AI tools know, by consent."""

from __future__ import annotations

import datetime
import json
from typing import Any

from infra.db_async import run_in_db, run_off_db
from tools.base import Tool

CONSENT_OPERATION = "agent_import_consent"
READ_OPERATION = "agent_import_read"
INSTALL_OPERATION = "agent_import_install"
INSTALL_WINDOW = datetime.timedelta(hours=1)
REFERENCE = "skill conductor-bootstrap's references/agent-import.md"

RAISE_FIRST = (
    "raise the import consent decision first: call agent_import with no "
    "consent_decision_id for its exact fields (in first run it is the import "
    "decision in setup.setup_decisions), draft it with plan_decision, "
    "parent_review and raise it, then call again with its id once the user answers"
)
NO_TOOLS = "no Claude Code, Codex, OpenClaw or Hermes folders were found on this Mac"
NO_MODEL = "the model this conversation runs on could not be resolved, so the consent cannot name it"
NONE_NAMED_FOUND = (
    "none of the tools the person named were found on this Mac (found: {found}); "
    "tell them, and raise the card for the found tools only if they ask for that"
)
NOT_HERE = (
    "agent_import is only for the CORSIA agent; tell the person to ask CORSIA to "
    "bring things over from their other AI tools"
)


def _now() -> datetime.datetime:
    return datetime.datetime.now(datetime.timezone.utc)


def _workspace():
    """The workspace whose ``skills/`` the offers compare against and installs write to."""
    from infra import config

    return config.WORKSPACE


def _texts():
    from onboarding.consent import ConsentTexts

    return ConsentTexts(
        raise_first=RAISE_FIRST,
        wrong_wording=(
            "that decision does not carry the fixed import consent wording for what is on "
            "this Mac now; " + RAISE_FIRST
        ),
        declined=(
            "the user did not choose to have their other AI tools read; do not read "
            "them and do not ask again unasked"
        ),
        used=(
            "that consent was already used for one read; raise a new import consent "
            "decision to read again"
        ),
    )


class AgentImportTool(Tool):
    """Read the person's other AI tools once per consent, then install picked skills."""

    @property
    def name(self) -> str:
        return "agent_import"

    @property
    def description(self) -> str:
        return (
            "Bring over what the person's Claude Code, Codex, OpenClaw or Hermes "
            "know: notes about them and how they like to work, as labelled quoted "
            "excerpts, and the skills in those tools' own skill folders, each with "
            "an offer state. Never their chats, settings, keys or passwords, and "
            "never a skill's body.\n\n"
            "USE THIS when the person asks to bring their things over from another "
            "AI tool, and in first run when setup.setup_decisions holds the import "
            f"decision. Follow {REFERENCE} for what to post.\n\n"
            "It needs the person's consent first: call it with no "
            "consent_decision_id for the exact consent_decision fields (pass tools "
            "to name only the ones they asked for), draft that decision with "
            "plan_decision, parent_review and raise it, then call mode read with "
            "its id once they answer. Only answer A allows a read, and one answer "
            "allows one. Then mode install with the item_ids they picked, from "
            "that read's new skills, within an hour. Each refusal names the step "
            "that is missing. Only the CORSIA agent can use it."
        )

    @property
    def parameters(self) -> dict[str, Any]:
        return {
            "type": "object",
            "properties": {
                "consent_decision_id": {
                    "type": "string",
                    "description": "Id of the answered consent decision in your plan. Omit it to get its fields.",
                },
                "mode": {
                    "type": "string", "enum": ["read", "install"],
                    "description": "read (the default) once per consent; install the picked skills after it.",
                },
                "item_ids": {
                    "type": "array", "items": {"type": "string"},
                    "description": "mode install: the item_id of each picked skill, from the read's skills list.",
                },
                "tools": {
                    "type": "array",
                    "items": {"type": "string", "enum": ["claude-code", "codex", "openclaw", "hermes"]},
                    "description": "With no consent_decision_id: name only these found tools on the card.",
                },
            },
        }

    async def execute(self, **kwargs: Any) -> str:
        from tools.delegation import _require_context

        context = _require_context(kwargs)
        if isinstance(context, str):
            return context
        thread_id, thread = context
        if not await run_in_db(_allowed, thread_id, thread.get("project_id")):
            return f"❌ {NOT_HERE}"
        facts = await run_in_db(_card_facts, thread_id)
        if isinstance(facts, str):
            return json.dumps({"error": facts})
        decision_id = str(kwargs.get("consent_decision_id") or "").strip()
        if not decision_id:
            return json.dumps(_consent_fields(facts, kwargs.get("tools"), thread_id=thread_id), sort_keys=True)
        from onboarding.consent import ConsentRefused

        try:
            if kwargs.get("mode") == "install":
                return json.dumps(await _install(thread_id, thread, decision_id, facts,
                                                 list(kwargs.get("item_ids") or [])), sort_keys=True)
            return json.dumps(await _read(thread_id, thread, decision_id, facts), sort_keys=True)
        except ConsentRefused as refusal:
            return f"❌ {refusal}"


def _allowed(thread_id: str, project_id: str | None) -> bool:
    """The CORSIA agent, or a workspace first-run thread before one is designated (G6)."""
    from infra.corsia_agent import is_corsia_agent
    from onboarding.setup_decisions import has_open_setup_task

    return is_corsia_agent(thread_id) or (project_id is None and has_open_setup_task(thread_id))


def _card_facts(thread_id: str):
    """The card's inputs for this conversation's model, or the error that stops it."""
    from api.provider_sources import list_sources
    from infra.database import get_db
    from onboarding import setup_decisions
    from onboarding.agent_import import found_tools
    from onboarding.project_discovery import Roots

    db = get_db()
    try:
        row = db.execute("SELECT model FROM threads WHERE id=?", (thread_id,)).fetchone()
    finally:
        db.close()
    if not found_tools(Roots.for_user()):
        return NO_TOOLS
    card = setup_decisions.import_card(row["model"] if row else "", setup_decisions.ready_sources(list_sources()))
    return card if card is not None else NO_MODEL


def _consent_fields(card, tools, *, thread_id: str | None = None) -> dict[str, Any]:
    from dataclasses import replace

    from onboarding import setup_decisions
    from onboarding.agent_import import LABELS

    named = tuple(LABELS[tool] for tool in tools or () if tool in LABELS)
    # A named tool that isn't here is reported, never widened to every found tool.
    missing = [tool for tool in named if tool not in card.tools]
    if named:
        kept = tuple(t for t in card.tools if t in named)
        if not kept:
            return {"error": NONE_NAMED_FOUND.format(found=", ".join(card.tools))}
        card = replace(card, tools=kept, connections=tuple(t for t in card.connections if t in named))
    consent = setup_decisions.agent_import(card)
    consent.pop("task_title")
    fields = {"error": RAISE_FIRST, "consent_decision": consent}
    if missing:
        fields["not_found"] = missing
    if thread_id is not None:
        _emit_event(thread_id, "consent_card", tools_count=len(card.tools))
    return fields


def _emit_event(thread_id: str, event: str, **counts: int | str) -> None:
    """Emit aggregate import evidence without exposing imported paths or content."""
    from infra.product_events import emit_product_event

    emit_product_event("agent_import", event, thread_id=thread_id, properties=counts)


def _actor(thread_id: str, thread: dict[str, Any]):
    from plans import public

    return public.PlanActor(thread_id=thread_id, user_id=thread["user_id"], project_id=thread["project_id"])


def _in_plan(job):
    """Run ``job(db)`` in one plan transaction on the DB pool."""
    def run():
        from infra.database import get_db
        from plans import public

        db = get_db()
        try:
            with public.plan_transaction(db):
                return job(db)
        finally:
            db.close()

    return run_in_db(run)


def _tool_keys(labels) -> list[str]:
    from onboarding.agent_import import LABELS

    return [key for key, label in LABELS.items() if label in labels]


async def _read(thread_id, thread, decision_id, card) -> dict[str, Any]:
    from onboarding import agent_import, agent_import_skills, consent, setup_decisions
    from onboarding.project_discovery import Roots

    wordings = dict(setup_decisions.agent_import_wordings(card))
    read_at = _now().isoformat(timespec="seconds")
    used = await _in_plan(lambda db: consent.consume(
        db, decision_id, _actor(thread_id, thread), list(wordings), _texts(),
        operation=CONSENT_OPERATION, result={"consumed": True, "read_at": read_at},
    ))
    tools = _tool_keys(wordings[used.wording])
    roots = Roots.for_user()
    memory = await run_off_db(agent_import.read_memory, roots, tools)
    skills = await run_off_db(agent_import_skills.skill_offers, roots, tools, _workspace())
    offered = [row["item_id"] for row in skills if row["state"] == agent_import_skills.NEW]
    await _in_plan(lambda db: consent.record_receipt(db, used, READ_OPERATION, {
        "tools": tools, "opened": memory.opened, "skipped": memory.skipped, "offers": offered,
    }))
    if memory.excerpts or skills:
        result = "read"
    else:
        result = "read_failed" if memory.failed else "nothing_found"
    skipped = sorted({str(row["path"]) for row in memory.skipped})
    _emit_event(thread_id, result, excerpts_count=len(memory.excerpts), skills_count=len(skills), skipped_count=len(skipped))
    return {"result": result, "excerpts": memory.excerpts, "skills": skills, "skipped_files": skipped,
            "install_by": (datetime.datetime.fromisoformat(read_at) + INSTALL_WINDOW).isoformat()}


def _authorize_install(db, used, item_ids, now):
    """The read's tools when ``item_ids`` may install now, or a refusal."""
    from onboarding import consent

    texts = _texts()
    spent = consent.load_receipt(db, used, CONSENT_OPERATION, texts)
    read = consent.load_receipt(db, used, READ_OPERATION, texts)
    if spent is None or read is None:
        raise consent.ConsentRefused("that consent has no finished read; call agent_import mode read with it first")
    if now - datetime.datetime.fromisoformat(spent["read_at"]) > INSTALL_WINDOW:
        raise consent.ConsentRefused(
            "installs from a read are allowed for one hour after it, and that hour has "
            "passed; raise a new import consent decision to read again"
        )
    missing = sorted(set(item_ids) - set(read["offers"]))
    if missing:
        raise consent.ConsentRefused(
            "these item_ids were not offered as new by that read: " + ", ".join(missing)
            + "; install only new item_ids from its skills list"
        )
    if consent.load_receipt(db, used, INSTALL_OPERATION, texts, {"item_ids": sorted(item_ids)}) is not None:
        raise consent.ConsentRefused("those skills were already installed from that read")
    return read["tools"]


async def _install(thread_id, thread, decision_id, card, item_ids) -> dict[str, Any]:
    from onboarding import agent_import_skills, consent, setup_decisions
    from onboarding.project_discovery import Roots

    if not item_ids:
        raise consent.ConsentRefused("pass the item_ids the person picked")
    now = _now()
    wordings = [wording for wording, _tools in setup_decisions.agent_import_wordings(card)]

    def authorize(db):
        used = consent.validate(db, decision_id, _actor(thread_id, thread), wordings, _texts())
        return used, _authorize_install(db, used, item_ids, now)

    used, tools = await _in_plan(authorize)
    results = await run_off_db(agent_import_skills.install_skills, Roots.for_user(), tools,
                               _workspace(), item_ids, now=now, author_thread_id=thread_id,
                               consent_decision_id=decision_id)
    await _in_plan(lambda db: consent.record_receipt(db, used, INSTALL_OPERATION, {
        "items": [{key: row[key] for key in ("item_id", "sha256", "outcome") if key in row} for row in results],
    }, {"item_ids": sorted(item_ids)}))
    for row in results:
        _emit_event(thread_id, "install", outcome=str(row["outcome"]), count=1)
    installed = sum(1 for row in results if row["outcome"] == "installed")
    return {"installed": installed, "results": results}
