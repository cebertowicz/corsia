"""Every Slack token family is masked, wherever it sits in a tool result."""
import json

import pytest

from core.corrections_input import mask
from host_access.credential_sanitizer import sanitize_credential_text

# Built at runtime so no literal token shape is committed.
_BODY = "7316209845-8204731960521-" + "Zq4mT9xVb2LcR8nWk5HyJ3pD"
SHAPES = {
    "bot": "xox" + "b-",
    "user": "xox" + "p-",
    "rotating access": "xox" + "e.xox" + "p-1-",
    "refresh": "xox" + "e-1-",
    "app-level": "xa" + "pp-1-",
    "legacy session": "xox" + "s-",
    "client": "xox" + "c-",
    "cookie": "xox" + "d-",
}
# Neutral surroundings, so only the token-shape rule can mask it.
WRAPPERS = {
    "plain": "copied {} from the page",
    "json": json.dumps({"value": "{}"}),
    "call argument": 'WebClient("{}")',
    "path": "https://slack.test/v/{}/next",
}


@pytest.mark.parametrize("prefix", SHAPES.values(), ids=SHAPES)
@pytest.mark.parametrize("wrapper", WRAPPERS.values(), ids=WRAPPERS)
def test_sanitizer_masks_every_slack_token_shape(prefix, wrapper):
    token = prefix + _BODY
    safe = sanitize_credential_text(wrapper.replace("{}", token))

    assert _BODY not in safe
    assert "[REDACTED]" in safe


@pytest.mark.parametrize("prefix", SHAPES.values(), ids=SHAPES)
def test_correction_mask_covers_every_slack_token_shape(prefix):
    assert _BODY not in mask("pasted " + prefix + _BODY + " here")


def test_short_slack_like_words_stay_readable():
    text = "xoxo-hugs and xapp-ready"
    assert sanitize_credential_text(text) == text
