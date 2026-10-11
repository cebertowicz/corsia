"""Hidden providers stay deletable but must not accept keys or report tests.

openrouter/deepseek/gemini/groq are advertised nowhere and have no dispatch leg
in llm.router. Keeping them addressable lets a user clear a stale saved key.
Accepting a NEW key, or returning a connectivity 'test' result, would tell the
user something works when any real call falls through to the poisoned-global 404.
"""

from __future__ import annotations

import pytest
from fastapi import HTTPException

from api.provider_meta import PROVIDER_META
from api.providers import _refuse_unsupported_provider

HIDDEN = [n for n, m in PROVIDER_META.items() if m.get("hidden")]
VISIBLE = [n for n, m in PROVIDER_META.items() if not m.get("hidden")]


def test_some_providers_are_hidden_and_some_are_not():
    assert HIDDEN, "expected unsupported cloud providers to be marked hidden"
    assert VISIBLE, "expected supported providers to remain visible"


@pytest.mark.parametrize("name", HIDDEN)
def test_hidden_provider_write_is_refused_with_the_supported_alternative(name):
    with pytest.raises(HTTPException) as excinfo:
        _refuse_unsupported_provider(name, PROVIDER_META[name])
    assert excinfo.value.status_code == 400
    detail = excinfo.value.detail
    assert "External provider" in detail, "must point at the supported path"
    assert "DELETE" in detail, "must say a stale key can still be removed"


@pytest.mark.parametrize("name", VISIBLE)
def test_supported_provider_is_untouched(name):
    _refuse_unsupported_provider(name, PROVIDER_META[name])


def test_unsupported_provider_with_a_saved_key_stays_listed():
    """Hiding a provider that still holds a key would strand that credential.

    The settings UI renders the provider list; if an unsupported provider with a
    saved key is omitted, the user has no control through which to delete it.
    """
    from api import providers

    hidden = HIDDEN[0]
    listed = providers._visible_providers({}, {hidden: "saved-secret-value-1234"})
    names = {entry["name"] for entry in listed}
    assert hidden in names, "a hidden provider holding a key must remain removable"
    entry = next(e for e in listed if e["name"] == hidden)
    assert entry["unsupported"] is True
    assert entry["has_key"] is True


def test_unsupported_provider_without_a_key_stays_hidden():
    from api import providers

    hidden = HIDDEN[0]
    listed = providers._visible_providers({}, {})
    assert hidden not in {entry["name"] for entry in listed}
