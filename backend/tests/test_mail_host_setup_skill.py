"""The System skill an agent follows to connect an email host other than Gmail or Microsoft,
and the first-run step that hands "something else" to it."""

from __future__ import annotations

import re
from pathlib import Path

from tools.catalog_mailhost_entries import MAILHOST_CATALOG_ENTRIES

SKILLS = Path(__file__).resolve().parents[1] / "prompts/system/v1/skills"
SKILL = (SKILLS / "mail-host-setup/SKILL.md").read_text(encoding="utf-8")
FIRST_RUN_APP = (SKILLS / "conductor-bootstrap/references/first-run-app.md").read_text(encoding="utf-8")


def test_the_skill_is_named_and_never_handles_a_password() -> None:
    front = SKILL.split("---")[1]
    assert "name: mail-host-setup" in front and "description: Use when the person wants email or calendar" in front
    assert "Never ask for, type, read, print or paste a password." in SKILL
    assert "Never open the host's app-password page with `browser_open`." in SKILL
    assert "never repeat it; tell them to delete that app password" in SKILL


def test_the_skill_hands_gmail_and_microsoft_to_their_own_skills_and_posts_the_offer_verbatim() -> None:
    assert "follow `microsoft-setup`" in SKILL and "follow `google-setup` from its step 2" in SKILL
    assert "I can look after your <host> mail and calendar. <host> needs an app password for that" in SKILL
    assert "Ask me any time to connect your email." in SKILL
    assert "press Set key on the <host> row in Settings › Connected apps, paste it and press Set" in SKILL


def test_every_tool_the_skill_names_exists_and_is_loaded_through_the_pack_it_lives_in() -> None:
    named = set(re.findall(r"`(mail_host_[a-z_]+)`", SKILL))
    assert named == {"mail_host_connect"}
    assert MAILHOST_CATALOG_ENTRIES["mail_host_connect"].category == "google"
    assert "Load pack `google` with `tools_load`" in SKILL


def test_first_run_hands_something_else_to_the_skill_after_the_profile_line_is_written() -> None:
    email = FIRST_RUN_APP.split("6. **Email and calendar.**", 1)[1].split("7. **Learning", 1)[0]
    assert "- **Something else:** follow skill `mail-host-setup` from its step 1." in email
    assert email.index("`- **Email app:**") < email.index("**Something else:**")
    assert "no ready way to connect that email" not in FIRST_RUN_APP
    assert "noted which one you use" not in FIRST_RUN_APP
