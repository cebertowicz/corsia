"""The System skill an agent follows to connect an email host other than Gmail or Microsoft,
and the first-run step that hands "something else" to it."""

from __future__ import annotations

import json
import re
from pathlib import Path

from tools.catalog_mailhost_entries import MAILHOST_CATALOG_ENTRIES

SKILLS = Path(__file__).resolve().parents[1] / "prompts/system/v1/skills"
SKILL = (SKILLS / "mail-host-setup/SKILL.md").read_text(encoding="utf-8")
FIRST_RUN_APP = (SKILLS / "conductor-bootstrap/references/first-run-app.md").read_text(encoding="utf-8")


def test_the_skill_is_named_and_never_handles_a_password() -> None:
    front = SKILL.split("---")[1]
    assert "name: mail-host-setup" in front and "the host is unknown or not Gmail or Microsoft" in front
    google_front = (SKILLS / "google-setup/SKILL.md").read_text(encoding="utf-8").split("---")[1]
    assert "description: Use only when Google refuses the sign-in from `google_connect`" in google_front
    assert "the person asks to connect using their own Google Cloud project" in google_front
    assert "Never ask for, type, read, print or paste a password." in SKILL
    assert "Never open the host's app-password page with `browser_open`." in SKILL
    assert "never repeat it; tell them to delete that app password" in SKILL


def test_the_skill_hands_gmail_and_microsoft_to_their_own_skills_and_posts_the_offer_verbatim() -> None:
    assert "follow `microsoft-setup`" in SKILL and "then `google_connect` and post its link" in SKILL
    assert "one Allow, no Google Cloud project" in SKILL
    assert "Use skill `google-setup` only if Google refuses that sign-in" in SKILL
    assert "I can look after your <host> mail and calendar. <host> needs an app password for that" in SKILL
    assert "<host> needs an app password for that, made on <host>'s site." in SKILL and "just for CORSIA" not in SKILL
    assert "Ask me any time to connect your email." in SKILL
    assert "I'll show you where; you paste it into Settings, never into chat. Go ahead?" in SKILL
    assert "Keys entry" not in SKILL
    assert "press Set key on the <host> row in Settings › Connected apps and paste it." in SKILL and "press Set." not in SKILL


def test_the_offer_comes_before_the_first_call_that_adds_the_account() -> None:
    steps = SKILL.split("## After an update", 1)[0]
    assert steps.index("Go ahead?") < steps.index("call `mail_host_connect`")
    step3 = steps.split("\n3. ", 1)[1].split("\n4. ", 1)[0]
    assert "Go ahead?" in step3 and "mail_host_connect" not in step3
    step2 = steps.split("\n2. ", 1)[1].split("\n3. ", 1)[0]
    assert "mail_host_connect" not in step2
    assert "Then apply step 1 to it." in step2


def test_a_system_change_notice_that_adds_the_skill_is_handled_once() -> None:
    assert "a System-change notice adds this skill" in SKILL.split("---")[1]
    after = SKILL.split("## After an update", 1)[1]
    assert "When a System-change notice adds this skill: if the profile says `Email app: Other`, offer once" in after
    # Every exit names its bulletin_disposition state: open offer, account added, declined, or no offer due.
    assert "record the notice `deferred` while the offer is open, `acted` once the account is added, or `not_applicable` if they say not now, and never offer again" in after
    assert "Otherwise record it `not_applicable`." in after


def test_every_tool_the_skill_names_exists_and_is_loaded_through_the_pack_it_lives_in() -> None:
    named = set(re.findall(r"`(mail_host_[a-z_]+)`", SKILL))
    assert named == {"mail_host_connect"}
    assert MAILHOST_CATALOG_ENTRIES["mail_host_connect"].category == "email"
    assert "Load pack `email` with `tools_load`" in SKILL


def test_first_run_hands_something_else_to_the_skill_after_the_profile_line_is_written() -> None:
    email = FIRST_RUN_APP.split("6. **Email and calendar.**", 1)[1].split("7. **Learning", 1)[0]
    assert "- **Something else:** follow skill `mail-host-setup` from its step 1." in email
    assert email.index("`- **Email app:**") < email.index("**Something else:**")
    assert "no ready way to connect that email" not in FIRST_RUN_APP
    assert "noted which one you use" not in FIRST_RUN_APP


def test_an_install_whose_baseline_lacks_the_skill_is_told_it_was_added() -> None:
    from infra import bulletins

    path = "skills/mail-host-setup/SKILL.md"
    current = bulletins.system_identity()
    assert path in dict(current.files)
    baseline = json.dumps([[name, digest] for name, digest in current.files if name != path])
    assert bulletins._identity_changes(baseline, current) == {"added": [path], "changed": [], "removed": []}
