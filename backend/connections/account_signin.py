"""One vendor sign-in as CORSIA holds it in memory, and the public reference a failed one is logged under."""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass, field
from typing import Any

from connections.corsia_agent_account import FirstRunBind
from infra.failure_reference import new_reference

log = logging.getLogger(__name__)


@dataclass
class SignIn:
    """One in-memory vendor sign-in, completed only against its starting revision.

    A finished one is kept, one per account, until that account's next start,
    sign-out or remove, or backend shutdown.
    """

    account_id: str
    provider: str
    revision: int
    status: str = "waiting"
    cause: str | None = None
    duplicate_of: str | None = None
    process: asyncio.subprocess.Process | None = None
    task: asyncio.Task | None = None
    prompt: dict[str, str] | None = None  # what the vendor asked the person to do, when its profile reads it
    prompted: asyncio.Event = field(default_factory=asyncio.Event)
    bind: FirstRunBind | None = None  # the first-run screen's conductor, bound once this login reads Ready
    ref: str | None = None  # a failed sign-in's public reference, logged beside its cause

    @property
    def held(self) -> bool:
        """Still waiting on the vendor; a hold that died without an answer is not."""
        return self.status == "waiting" and not (self.task is not None and self.task.done())

    def public(self) -> dict[str, Any]:
        """The sign-in as shown to the user: never the vendor's output, only a prompt its profile matched."""
        shown = {"account_id": self.account_id, "route": "browser", "status": self.status, "cause": self.cause}
        if self.ref is not None:
            shown["ref"] = self.ref
        if self.prompt is not None and self.status == "waiting":
            shown |= self.prompt
        if self.status == "completed" and self.bind is not None:
            shown |= self.bind.public()
        # Completed and same-account refused sign-ins name their match.
        return (shown | {"duplicate_of": self.duplicate_of}
                if self.status == "completed" or self.cause == "same_account" else shown)


def failure_reference(provider: str, cause: str | None) -> str:
    """Log a failed sign-in once, under the reference its row shows and a setup report quotes."""
    ref = new_reference("signin")
    log.warning("%s sign-in failed reference=%s cause=%s", provider, ref, cause)
    return ref
