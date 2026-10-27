"""The two kinds of page the person completes; browser_step_lifecycle owns their lifecycle.

A password step opens when the agent's own probe finds a visible password field, and stays open while
one shows on its visit. A person step opens when the agent hands the person a page with no password
field (an account chooser, a passkey or phone prompt), and stays open for its whole visit. Each is one
page visit's step in the agent's plan; neither keeps any page value or text.
"""
from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from typing import ClassVar

from host_access import browser_pane_state
from host_access.browser_domain import registrable_domain
from infra import state

PASSWORD_STEP_DESCRIPTION = (
    "Enter your password in the shared browser. The agent can't see this page and carries on once you're past it.")
# The plan states a person's own Mark done leaves (``plans.node_states``); any other ending is a decline.
# Local copy until plans.public exports its accepted states (#1481); the equality test pins it meanwhile.
DONE_STATES = frozenset({"accepted", "accepted_with_followup"})


@dataclass(eq=False)
class BrowserStep:
    """One page visit the person completes in the shared browser."""

    visit: str
    host: str
    # The page target the step's visit is on; a probe of any other target never settles it.
    target: str = ""
    node_id: str | None = None
    session: str | None = field(default=None, kw_only=True)
    occurrence: int = field(default=1, kw_only=True)
    # The step's one re-probe loop, and the person's latest input that it waits to pause (AR6).
    reprobe: asyncio.Task | None = field(default=None, repr=False)
    last_input: float | None = None
    input_seen: asyncio.Event = field(default_factory=asyncio.Event, repr=False)

    provenance: ClassVar[str]
    event_prefix: ClassVar[str]
    settlement_evidence: ClassVar[str]

    def domain(self) -> str:
        # AR5: a title names the live page's registrable domain, the one the pane's address keeps visible.
        return registrable_domain(self.host) or "this site"

    def title(self) -> str:
        raise NotImplementedError

    def description(self) -> str:
        raise NotImplementedError

    def ended(self, thread_id: str) -> None:
        """Undo what the step showed beyond its plan row, once it has ended."""


@dataclass(eq=False)
class PasswordStep(BrowserStep):
    """A visible password field the agent's probe found."""

    provenance = "browser.password_guard"
    event_prefix = "password_step"
    settlement_evidence = "Password field no longer on the page"

    def title(self) -> str:
        return f"Enter your password in the shared browser ({self.domain()})"

    def description(self) -> str:
        return PASSWORD_STEP_DESCRIPTION

    def shown(self, thread_id: str) -> None:
        publish_password_page(thread_id, True)

    def ended(self, thread_id: str) -> None:
        publish_password_page(thread_id, False)


@dataclass(eq=False)
class PersonStep(BrowserStep):
    """A page the agent declared the person's to finish; the person keeps it for its whole visit."""

    instruction: str = field(kw_only=True)

    provenance = "browser.person_step"
    event_prefix = "person_step"
    settlement_evidence = "The shared browser moved on to a new page."

    def title(self) -> str:
        return f"Your turn in the shared browser ({self.domain()})"

    def description(self) -> str:
        return (
            "The agent can't see this page and carries on once you're past it."
            f"\n\n{self.instruction}"
        )


def publish_password_page(thread_id: str, visible: bool) -> None:
    """Show or clear the password notice on the thread's pages and panes."""
    message = {"type": "browser_password_page", "thread_id": thread_id, "visible": visible}
    state.broadcast_to_thread(thread_id, message)
    browser_pane_state.report(thread_id, message)
