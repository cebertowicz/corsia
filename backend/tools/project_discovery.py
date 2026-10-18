"""``project_discovery_list``: the folders the user has worked in, by consent."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from infra.db_async import run_in_db, run_off_db
from tools.base import Tool


class ProjectDiscoveryListTool(Tool):
    """List the user's Claude Code and Codex working folders, once per consent."""

    @property
    def name(self) -> str:
        return "project_discovery_list"

    @property
    def description(self) -> str:
        return (
            "List the folders the user has been working in, from the start "
            "of each of their Claude Code and Codex session files: path, "
            "name, last used, which tool, and whether it is a git repo; newest "
            "first, at most 50. Never any conversation text.\n\n"
            "USE THIS when the user asked you to find or set up the projects "
            "they already work on.\n\n"
            "It needs the user's consent first: call it with no "
            "consent_decision_id to get the exact consent_decision fields, "
            "draft that decision with plan_decision, parent_review and raise "
            "it, then call this with its id once the user answers. Only answer "
            "A allows a listing, and one answer allows one. Each refusal names "
            "the step that is missing.\n\n"
            "During first run the no-id call returns setup_decisions instead, "
            "the ordered setup decisions CORSIA built from facts it read (raise "
            "each unchanged, from its task_title's task), and "
            "setup_task_settlements. There the consent is the find-projects decision "
            "for 'Set up your first project': pass its id. "
            "Pass self_check_passed=false when corsia_api could not be reached."
        )

    @property
    def parameters(self) -> dict[str, Any]:
        return {
            "type": "object",
            "properties": {
                "consent_decision_id": {
                    "type": "string",
                    "description": (
                        "Id of the answered consent decision in your plan. "
                        "Omit it to get that decision's fields."
                    ),
                },
                "self_check_passed": {
                    "type": "boolean",
                    "description": (
                        "First run only, with no consent_decision_id: false "
                        "when corsia_api failed, so the oMLX item "
                        "is left out. Default true."
                    ),
                },
            },
        }

    async def execute(self, **kwargs: Any) -> str:
        from onboarding import project_discovery
        from onboarding.consent import ConsentRefused
        from tools.delegation import _require_context

        context = _require_context(kwargs)
        if isinstance(context, str):
            return context
        thread_id, thread = context
        from onboarding import setup_decisions

        roots = project_discovery.Roots.for_user()
        decision_id = str(kwargs.get("consent_decision_id") or "").strip()
        if not decision_id:
            if await run_in_db(setup_decisions.has_open_setup_task, thread_id):
                payload = {"error": project_discovery.RAISE_FIRST}
                payload.update(await setup_decisions.setup_plan(
                    self_check_passed=kwargs.get("self_check_passed") is not False,
                ))
                return json.dumps(payload, sort_keys=True)
            apps = tuple(await run_off_db(project_discovery.session_apps, roots))
            consent = setup_decisions.find_projects(apps)
            if consent is None:
                return json.dumps({"error": project_discovery.NO_SESSIONS})
            consent.pop("task_title")
            return json.dumps(
                {"error": project_discovery.RAISE_FIRST, "consent_decision": consent},
                sort_keys=True,
            )

        wordings = await run_off_db(_current_wordings, roots)

        def _consume() -> str | None:
            from infra.database import get_db
            from plans import public

            actor = public.PlanActor(
                thread_id=thread_id,
                user_id=thread["user_id"],
                project_id=thread["project_id"],
            )
            db = get_db()
            try:
                with public.plan_transaction(db):
                    project_discovery.consume_consent(db, decision_id, actor, wordings)
            except ConsentRefused as refusal:
                return str(refusal)
            finally:
                db.close()
            return None

        refusal = await run_in_db(_consume)
        if refusal is not None:
            return f"❌ {refusal}"

        from infra import config

        folders = await run_off_db(
            project_discovery.discover, roots, excluded=_corsia_roots(),
        )
        return json.dumps({"folders": folders, "count": len(folders)}, sort_keys=True)


def _corsia_roots() -> tuple[Path, ...]:
    """CORSIA's own folders: sessions started in them are not the person's projects."""
    from infra import config

    return (
        config.WORKSPACE,
        config.STATE_ROOT,
        Path(config.__file__).resolve().parents[2],
        config.BOOTSTRAP_CONFIG_PATH.parent,
    )


def _current_wordings(roots) -> list[tuple[str, str, str]]:
    """What a valid consent must carry now."""
    from onboarding import project_discovery, setup_decisions

    return setup_decisions.consent_wordings(tuple(project_discovery.session_apps(roots)))
