"""Model-visible host-access denials raised by the host-action gate."""

from __future__ import annotations


class HostAccessGateError(PermissionError):
    """A safe model-visible host-access denial."""


class HostAccessRequired(HostAccessGateError):
    """The action needs a reviewed lease but none is attributable."""


class HostAccessBlocked(HostAccessGateError):
    """The action or its reviewer failed closed."""
