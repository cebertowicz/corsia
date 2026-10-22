"""Typed launch and authentication failures shared by disposable fork adapters."""

from core.fork_types import ProviderForkRejected


class ForkBinaryResolutionError(ProviderForkRejected, ValueError):
    """The selected harness executable could not be resolved or launched."""


class ForkCredentialsUnavailable(ProviderForkRejected):
    """A typed provider or launch result proves credentials are unavailable."""
