"""What the first-run wake tells the conductor: its summary and instruction text.

The text comes from skill ``conductor-bootstrap``'s references. Once inlined into a
message it has no folder of its own, so each reference it names is given the path
agents read it from, the workspace projection.
"""

from __future__ import annotations

import re
from typing import Any

SKILL_DIR = "skills/conductor-bootstrap"
SKILL_PROJECTION = ".corsia/" + SKILL_DIR
FIRST_RUN_REFERENCE = f"{SKILL_DIR}/references/first-run.md"
APP_REFERENCE = f"{SKILL_DIR}/references/first-run-app.md"
APP_START_REFERENCE = f"{SKILL_DIR}/references/first-run-start.md"
_BARE_REFERENCE = re.compile(r"(?<![\w./-])references/[\w.-]+\.md")

INLINE_SUMMARY = (
    "First run of a new install. Your instructions are below: "
    f"{SKILL_PROJECTION}/references/first-run.md. The Setup facts section "
    "holds what its Preparation reads; do not load the skill or call those "
    "tools for them. A fact marked unavailable names the one read that gets it."
)
APP_INLINE_SUMMARY = (
    "First run of a new install. Your instructions are below: "
    f"{SKILL_PROJECTION}/references/first-run-start.md. The server has already "
    "posted your opening and seeded your plan. Start the first applicable step."
)


def app_reference_served() -> bool:
    """Whether the app-path instruction reference is present in this System package."""
    from infra.system_package import system_resource_source
    from onboarding.first_run_opening import OPENING_REFERENCE

    return all(system_resource_source(path).is_file() for path in (APP_REFERENCE, APP_START_REFERENCE, OPENING_REFERENCE))


def instructions(path: str = "script") -> str:
    """The script instructions, or the app's compact first-turn instructions."""
    from infra.system_package import system_resource_source

    if path == "script":
        return system_resource_source(FIRST_RUN_REFERENCE).read_text(encoding="utf-8").strip()
    if path == "app":
        return system_resource_source(APP_START_REFERENCE).read_text(encoding="utf-8").strip()
    raise ValueError("first_run_path must be 'app' or 'script'")


def anchored(text: str) -> str:
    """Give this skill's bare ``references/`` names their workspace path.

    Another skill's reference (skill ``github`` › ``references/agent-account.md``)
    is not in this skill's folder, so it stays as written.
    """
    from infra.system_package import system_resource_source

    def full(match: re.Match[str]) -> str:
        own = system_resource_source(f"{SKILL_DIR}/{match[0]}").is_file()
        return f"{SKILL_PROJECTION}/{match[0]}" if own else match[0]

    return _BARE_REFERENCE.sub(full, text)


def first_app_step(facts: dict[str, Any]) -> str:
    """Inline only the first applicable numbered section; keep later steps on demand."""
    from infra.system_package import system_resource_source
    from onboarding.app_first_run import applicable_steps

    steps = applicable_steps(facts)
    number = steps[0]["step_number"] if steps else 9
    reference = system_resource_source(APP_REFERENCE).read_text(encoding="utf-8")
    if number == 9:
        return reference[reference.index("## Hand over to your agents"):].strip()
    match = re.search(rf"(?ms)^{number}\. \*\*.*?(?=^\d+\. \*\*|^Silently,|\Z)", reference)
    if match is None:
        raise ValueError(f"The first-run app reference has no step {number}")
    section = match.group().strip()
    if number == 3:
        # The first question needs no helper/connection instructions until answered.
        section = section.split("   - **Quick**", 1)[0].strip()
    elif number == 4:
        web = system_resource_source(f"{SKILL_DIR}/references/web-search.md").read_text(encoding="utf-8")
        section += "\n\n" + web.split("## 1. Explain, then ask", 1)[1].split("## 2.", 1)[0].strip()
    elif number == 5:
        lookup = system_resource_source(f"{SKILL_DIR}/references/look-up-the-person.md").read_text(encoding="utf-8")
        section += "\n\n" + lookup.split("## Before anything: ask when search was already set up", 1)[1].split("## A.", 1)[0].strip()
    return "## Your first step\n\n" + section
