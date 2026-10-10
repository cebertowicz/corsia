"""Refusals a durable document write can raise."""

from __future__ import annotations


class DurableWritePolicyError(PermissionError):
    """An ordinary write violates the canonical shared-directory contract."""


class SharedIdentitySetupRequired(DurableWritePolicyError):
    """A live decision needs identity setup outside the started write."""

    def __init__(self) -> None:
        super().__init__("This document is being set up; try again")


class ThreadUnavailableError(DurableWritePolicyError):
    """The acting thread no longer exists, so no scope can be resolved for it."""


class DocumentOwnershipRefused(DurableWritePolicyError):
    """Only the document's owner, the conductor or the user may edit it (DCOL-FR5.9).

    Final on every path: unlike workspace organization, a host-action review
    never waives it.
    """
