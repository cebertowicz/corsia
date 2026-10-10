"""Every cloud provider the settings UI advertises must actually work.

A provider is only honest to advertise when both halves exist: catalog
discovery can enumerate its models, and the router has a dispatch leg for its
model prefix. Providers failing either half are marked ``hidden`` so the UI
stops offering a choice that cannot complete.
"""

from __future__ import annotations

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from api.provider_meta import PROVIDER_META  # noqa: E402
from llm.capabilities import _CLOUD_PROVIDERS  # noqa: E402

#: Providers with a real ``discover_cloud_models`` branch AND a router leg.
_WORKING_CLOUD = {"anthropic", "openai"}


def _advertised_cloud() -> set[str]:
    return {
        name
        for name, meta in PROVIDER_META.items()
        if meta.get("type") == "cloud" and not meta.get("hidden")
    }


def test_advertised_cloud_providers_are_exactly_the_working_ones():
    assert _advertised_cloud() == _WORKING_CLOUD


def test_every_advertised_cloud_provider_has_a_dispatch_prefix():
    routable = {prefix.rstrip("/") for prefix in _CLOUD_PROVIDERS}
    assert _advertised_cloud() <= routable


def test_hidden_providers_remain_addressable_for_key_cleanup():
    """Hiding must not orphan a key a user already saved."""
    for name in ("openrouter", "deepseek", "gemini", "groq"):
        assert name in PROVIDER_META, f"{name} must stay addressable by name"
        assert PROVIDER_META[name]["hidden"] is True
