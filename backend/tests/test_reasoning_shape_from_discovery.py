"""Reasoning shapes built from what an oMLX server reported at discovery.

Replaces a model-name matcher: the server reads each model's chat template and
reports what it accepts, so capability no longer depends on a filename.
"""

from llm.catalog_network_models import _apply_admin_model, _parse_omlx_status
from llm.reasoning_config import reasoning_shape_from_discovery


class _Response:
    status_code = 200

    def __init__(self, payload):
        self._payload = payload

    def json(self):
        return self._payload


# Field subsets captured from the live server on this machine (oMLX 0.6.4,
# 2026-09-02): the public status entry and the admin entry for the same model.
_STOCK_STATUS_FLASH_NEXT = {
    "id": "Qwen3.8-Flash-Next-MLX-oQ8-MTP",
    "loaded": True,
    "engine_type": "vlm",
    "config_model_type": "qwen4_exp",
    "estimated_size": 135890000000,
    "model_context_length": 262144,
    "max_context_window": 262144,
    "thinking_default": True,
    "preserve_thinking_default": True,
}
_STOCK_ADMIN_FLASH_NEXT = {
    "id": "Qwen3.8-Flash-Next-MLX-oQ8-MTP",
    "thinking_default": True,
    "preserve_thinking_default": True,
    "settings": {
        "enable_thinking": True,
        "chat_template_kwargs": {"reasoning_effort": "xhigh", "preserve_thinking": True},
    },
}
# The same model as reported by the patched server (branch under upstream
# review), verified against the real checkpoint: the template's vocabulary.
_PATCHED_EXTRA = {"reasoning_effort_options": ["xhigh", "medium", "low"], "reasoning_effort_default": "xhigh"}


def _shape_from(status_entry: dict, admin_entry: dict | None) -> dict | None:
    canonical = _parse_omlx_status(_Response({"models": [status_entry]}))
    if admin_entry is not None:
        _apply_admin_model(canonical, admin_entry)
    return reasoning_shape_from_discovery(canonical[status_entry["id"]])


def test_stock_server_payload_degrades_to_a_toggle():
    """Today's server reports no vocabulary, so Flash-Next gets On/Off, not an error."""
    shape = _shape_from(_STOCK_STATUS_FLASH_NEXT, _STOCK_ADMIN_FLASH_NEXT)
    assert shape == {"type": "boolean", "default": True}


def test_stock_server_without_admin_access_still_degrades_to_a_toggle():
    shape = _shape_from(_STOCK_STATUS_FLASH_NEXT, None)
    assert shape == {"type": "boolean", "default": True}


def test_patched_server_payload_lights_up_the_levels():
    """Once the server reports its vocabulary, the same model gets a level picker."""
    shape = _shape_from(
        {**_STOCK_STATUS_FLASH_NEXT, **_PATCHED_EXTRA},
        {**_STOCK_ADMIN_FLASH_NEXT, **_PATCHED_EXTRA},
    )
    assert shape == {
        "type": "effort_levels",
        "values": ["xhigh", "medium", "low"],
        "default": "xhigh",
    }


def test_patched_server_picker_opens_on_the_configured_level():
    admin = {**_STOCK_ADMIN_FLASH_NEXT, **_PATCHED_EXTRA}
    admin["settings"] = {**admin["settings"], "chat_template_kwargs": {"reasoning_effort": "low"}}
    shape = _shape_from({**_STOCK_STATUS_FLASH_NEXT, **_PATCHED_EXTRA}, admin)
    assert shape["default"] == "low"


def test_declared_levels_become_an_effort_picker():
    shape = reasoning_shape_from_discovery(
        {
            "id": "qwen38",
            "reasoning_effort_options": ["xhigh", "medium", "low"],
            "reasoning_effort_default": "xhigh",
            "thinking_default": True,
        }
    )
    assert shape == {
        "type": "effort_levels",
        "values": ["xhigh", "medium", "low"],
        "default": "xhigh",
    }


def test_picker_opens_on_the_servers_configured_level():
    """A new conversation starts from what the server is actually set to."""
    shape = reasoning_shape_from_discovery(
        {
            "reasoning_effort_options": ["low", "high", "max"],
            "reasoning_effort_default": "low",
            "configured_reasoning_effort": "high",
        }
    )
    assert shape["default"] == "high"


def test_configured_level_outside_the_vocabulary_is_ignored():
    shape = reasoning_shape_from_discovery(
        {
            "reasoning_effort_options": ["low", "high"],
            "reasoning_effort_default": "low",
            "configured_reasoning_effort": "bogus",
        }
    )
    assert shape["default"] == "low"


def test_falls_back_to_first_level_when_no_default_is_usable():
    shape = reasoning_shape_from_discovery(
        {"reasoning_effort_options": ["low", "high"], "reasoning_effort_default": None}
    )
    assert shape["default"] == "low"


def test_thinking_without_levels_becomes_a_toggle():
    shape = reasoning_shape_from_discovery(
        {"thinking_default": True, "reasoning_effort_options": []}
    )
    assert shape == {"type": "boolean", "default": True}


def test_toggle_opens_on_the_servers_current_setting():
    """The server's explicit override wins over the template's default."""
    shape = reasoning_shape_from_discovery(
        {"thinking_default": True, "configured_enable_thinking": False}
    )
    assert shape == {"type": "boolean", "default": False}


def test_model_without_thinking_gets_no_control():
    assert reasoning_shape_from_discovery({"thinking_default": None}) is None


def test_server_too_old_to_report_gets_no_control():
    """Absent fields mean 'cannot say', which must not become a capability claim."""
    assert reasoning_shape_from_discovery({"id": "some-model"}) is None


def test_levels_win_over_the_toggle():
    shape = reasoning_shape_from_discovery(
        {"thinking_default": True, "reasoning_effort_options": ["low", "high"]}
    )
    assert shape["type"] == "effort_levels"
