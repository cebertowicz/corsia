"""A sign-in step's title always fits the plan row's title cap, so the row is always written.

The plan refuses a node title over ``MAX_NODE_TITLE``; the step's wording is fixed and names the page's
domain, so a long domain is clipped to leave the title exactly the cap.
"""
from __future__ import annotations

import pytest

from host_access import browser_control, browser_steps, browser_step_lifecycle as lifecycle
from host_access.browser_cdp import PasswordProbe
from host_access.browser_session_state import BrowserSessionError
from plans.public import MAX_NODE_TITLE
from tests.test_browser_person_step import PAGE, THREAD, Page, _steps, plan  # noqa: F401

SUFFIX = ".org"
LONG_HOST = "longersitename12" + SUFFIX  # a 20-character registrable domain

KINDS = {
    "password": (lambda host: browser_steps.PasswordStep("visit", host),
                 "Enter your password in the shared browser ({})"),
    "person": (lambda host: browser_steps.PersonStep("visit", host, instruction="Choose your account"),
               "Your turn in the shared browser ({})"),
}


def _host(length: int) -> str:
    """A host that is its own registrable domain, ``length`` characters long."""
    return "a" * (length - len(SUFFIX)) + SUFFIX


@pytest.mark.parametrize("kind", KINDS)
def test_a_domain_that_just_fits_keeps_the_wording_unclipped(kind):
    make, template = KINDS[kind]
    room = MAX_NODE_TITLE - len(template.format(""))
    host = _host(room)
    title = make(host).title()
    assert title == template.format(host)
    assert len(title) == MAX_NODE_TITLE


@pytest.mark.parametrize("kind", KINDS)
def test_a_domain_one_character_over_is_clipped_from_its_start_to_exactly_the_cap(kind):
    """The one-over boundary: 61 characters unclipped, so 60 with the ellipsis in front."""
    make, template = KINDS[kind]
    room = MAX_NODE_TITLE - len(template.format(""))
    host = _host(room + 1)
    title = make(host).title()
    assert title == template.format("…" + host[-(room - 1):])
    assert len(title) == MAX_NODE_TITLE
    assert title.endswith(SUFFIX + ")")


@pytest.mark.parametrize("kind", KINDS)
def test_a_host_with_no_registrable_domain_is_clipped_too(kind):
    make, _template = KINDS[kind]
    title = make("[2001:db8:85a3:8d3:1319:8a2e:370:7348]").title()
    assert len(title) == MAX_NODE_TITLE
    assert title.endswith(":7348])")


@pytest.mark.parametrize("kind", KINDS)
@pytest.mark.parametrize("host, site_end", [
    ("accounts.google.com.secure-login-evil-phishing-site.com", "site.com"),
    ("accounts.google.com.secure-login-evil-phishing-site.example", "site.example"),
])
def test_a_phishing_shaped_host_keeps_the_end_that_names_the_site(kind, host, site_end):
    """The trusted-looking name sits on the left of a phishing host; the clip must drop it, never the right end."""
    make, _template = KINDS[kind]
    title = make(host).title()
    assert len(title) == MAX_NODE_TITLE
    assert title.endswith(site_end + ")")
    assert "google" not in title
    assert "(…" in title


@pytest.mark.asyncio
@pytest.mark.parametrize("kind", ["password", "person"])
async def test_a_long_domain_still_writes_the_row_and_the_step_is_held(plan, kind):  # noqa: F811
    """Through the real guard, plan write and title cap: the row exists and the page is refused as held."""
    page, db = Page(), plan["db"]
    page.now = PasswordProbe("visit-long", LONG_HOST, frozenset({42}) if kind == "password" else frozenset(),
                             PAGE, present_nodes=frozenset({42}) if kind == "password" else frozenset())
    if kind == "password":
        with pytest.raises(BrowserSessionError, match="asks for a password") as refused:
            await browser_control.require_page_access(THREAD, page.agent_probe, ("snapshot",))
        assert "couldn't be recorded" not in refused.value.args[0]
    else:
        await browser_control.declare_person_step(THREAD, page.agent_probe, "Choose your account")
    assert lifecycle.has_step(THREAD)
    [step] = _steps(db)
    assert len(step["title"]) <= MAX_NODE_TITLE
    assert "sitename12.org)" in step["title"]
