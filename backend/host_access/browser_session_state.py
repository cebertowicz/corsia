"""Private state types and errors for one owned native browser session."""
from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
import time
from typing import Literal

from host_access import browser_cli, browser_identity as identity, browser_reporting
from host_access.browser_chrome import ChromeProcess

BrowserTerminalState = Literal["running", "resetting", "closed", "death_unverified"]


@dataclass(frozen=True, slots=True)
class BrowserSessionRecord:
    """One backend-lifetime ownership binding, never reconstructed from a PID alone."""

    thread_id: str = field(repr=False)
    session: str = field(repr=False)
    lease_id: str | None = field(default=None, repr=False)
    show_window: bool = field(default=False, repr=False)
    shared: bool = field(default=False, repr=False)
    admitted_at: float = field(default_factory=time.time, repr=False)
    binding: identity.DaemonBinding | None = field(default=None, repr=False)
    terminal_state: BrowserTerminalState = "running"
    # The saved sign-in profile this session launches with, or None when it runs on a throwaway profile.
    profile: str | None = field(default=None, repr=False)
    # The thread whose open session holds that profile; this session starts from a copy of its sign-in.
    copy_of: str | None = field(default=None, repr=False)
    profile_notice: str | None = field(default=None, repr=False)

    @property
    def pid(self) -> int | None:
        """Read the daemon PID from its canonical immutable binding."""
        return self.binding.pid if self.binding is not None else None


@dataclass(slots=True)
class BrowserRuntime:
    """Mutable command and cleanup state belonging to one session record."""

    record: BrowserSessionRecord
    command_lock: asyncio.Lock = field(default_factory=asyncio.Lock)
    # Serializes a guard and its command without holding the lifecycle lock during cleanup.
    action_lock: asyncio.Lock = field(default_factory=asyncio.Lock)
    chrome: ChromeProcess | None = None
    profile_notice_sent: bool = False
    attempt_started_at: float | None = None
    attempt: browser_cli.CommandResult | None = None
    close_task: asyncio.Task[None] | None = None
    report: browser_reporting.CleanupNotice | None = None
    report_task: asyncio.Task[browser_reporting.CleanupNotice] | None = None


class BrowserSessionError(RuntimeError):
    """A browser session could not be admitted or safely used."""


class BrowserSetupError(BrowserSessionError):
    """Installation, version or mode refusal before any session effect."""

    @property
    def tool_message(self) -> str:
        """Name the real cause and its fix; nothing was started, so nothing needs cleanup."""
        return (f"Error: browser tools are not set up: {self}. No browser session was started, so nothing "
                "needs cleanup. Tell the user this setup step; browser tools work once it is fixed.")


class BrowserSessionCleanupError(BrowserSessionError):
    """Unresolved ownership with an explicit next action, never closure evidence."""

    def __init__(self, message: str, *, recovery: Literal["pending", "operator_required"] = "operator_required") -> None:
        super().__init__(message)
        self.recovery = recovery

    @property
    def tool_message(self) -> str:
        """Give the agent a safe action without exposing runtime identity."""
        if self.recovery == "pending":
            return "Error: browser cleanup is still pending. Allow the retained client or acknowledged daemon cleanup to finish before a later browser_close; do not repeatedly retry."
        return ("Error: browser cleanup could not be verified, so browser tools are blocked in this conversation. "
                "Stop retrying browser tools and inform the user: close any leftover agent-browser process or "
                "the Chrome window it opened, then restart CORSIA to clear the block. One later browser_close "
                "also clears it if the browser can still confirm closure. Do not kill processes yourself.")
