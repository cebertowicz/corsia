"""Reasoning shapes built from oMLX's template-derived discovery fields.

The Qwen fixture is shaped like the oMLX 0.7.1rc1 admin response. Its levels
come from ``detect_reasoning_effort`` run against the checkpoint template at
https://huggingface.co/Jundot/Qwen3.8-27B-oQ4e-mtp/tree/04dc5509edd8670fc78cc8c2f74bf9b77b1f2acc.
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
# oMLX 0.7.1rc1's admin row. ``reasoning_effort_options`` is a generic
# custom-control list; only ``reasoning_efforts`` is derived from this
# checkpoint's template.
_RC1_QWEN_ADMIN = {
    "reasoning_efforts": ["low", "medium", "xhigh"],
    "reasoning_effort_options": ["low", "medium", "high", "xhigh", "max"],
    "reasoning_effort_default": "low",
    "reasoning_effort_custom": True,
}


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


def test_rc1_template_efforts_offer_only_the_qwen_levels():
    shape = _shape_from(
        _STOCK_STATUS_FLASH_NEXT,
        {**_STOCK_ADMIN_FLASH_NEXT, **_RC1_QWEN_ADMIN},
    )
    assert shape == {
        "type": "effort_levels",
        "values": ["low", "medium", "xhigh"],
        "default": "xhigh",
    }


def test_rc1_picker_opens_on_the_configured_level():
    admin = {**_STOCK_ADMIN_FLASH_NEXT, **_RC1_QWEN_ADMIN}
    admin["settings"] = {**admin["settings"], "chat_template_kwargs": {"reasoning_effort": "low"}}
    shape = _shape_from(_STOCK_STATUS_FLASH_NEXT, admin)
    assert shape["default"] == "low"


def test_toggle_only_template_gets_a_boolean_control():
    shape = _shape_from(
        {"id": "gemma4", "thinking_default": True},
        {"id": "gemma4", "thinking_default": True, "reasoning_efforts": None},
    )
    assert shape == {"type": "boolean", "default": True}


def test_old_generic_custom_list_does_not_become_a_picker():
    shape = _shape_from(
        {"id": "old-omlx", "thinking_default": True},
        {
            "id": "old-omlx",
            "thinking_default": True,
            "reasoning_effort_options": ["low", "medium", "high", "xhigh", "max"],
            "reasoning_effort_custom": True,
        },
    )
    assert shape == {"type": "boolean", "default": True}


def test_rc1_empty_efforts_follow_omlx_generic_levels_contract():
    shape = reasoning_shape_from_discovery(
        {
            "thinking_default": True,
            "reasoning_efforts": [],
            "reasoning_effort_options": ["low", "medium", "high", "xhigh", "max"],
        }
    )
    assert shape["values"] == ["low", "medium", "high", "xhigh", "max"]


def test_rc1_shape_maps_saved_high_and_max_before_sending(monkeypatch):
    from llm import reasoning_config as rc

    shape = _shape_from(
        _STOCK_STATUS_FLASH_NEXT,
        {**_STOCK_ADMIN_FLASH_NEXT, **_RC1_QWEN_ADMIN},
    )
    monkeypatch.setattr(rc, "reasoning_config_for_model", lambda _: shape)
    assert rc.effective_reasoning_effort("high", "qwen38") == "xhigh"
    assert rc.effective_reasoning_effort("max", "qwen38") == "xhigh"


def test_declared_levels_become_an_effort_picker():
    shape = reasoning_shape_from_discovery(
        {
            "id": "qwen38",
            "reasoning_efforts": ["xhigh", "medium", "low"],
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
            "reasoning_efforts": ["low", "high", "max"],
            "reasoning_effort_default": "low",
            "configured_reasoning_effort": "high",
        }
    )
    assert shape["default"] == "high"


def test_configured_level_outside_the_vocabulary_is_ignored():
    shape = reasoning_shape_from_discovery(
        {
            "reasoning_efforts": ["low", "high"],
            "reasoning_effort_default": "low",
            "configured_reasoning_effort": "bogus",
        }
    )
    assert shape["default"] == "low"


def test_falls_back_to_first_level_when_no_default_is_usable():
    shape = reasoning_shape_from_discovery(
        {"reasoning_efforts": ["low", "high"], "reasoning_effort_default": None}
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
        {"thinking_default": True, "reasoning_efforts": ["low", "high"]}
    )
    assert shape["type"] == "effort_levels"


def test_managed_omlx_defaults_an_unset_effort_to_medium(monkeypatch):
    from llm import reasoning_config as rc

    shape = reasoning_shape_from_discovery(
        {
            "reasoning_efforts": ["low", "medium", "xhigh"],
            "reasoning_effort_default": "low",
        },
        default_effort_when_unset="medium",
    )

    assert shape == {
        "type": "effort_levels",
        "values": ["low", "medium", "xhigh"],
        "default": "medium",
        "applies_default_when_unset": True,
    }
    monkeypatch.setattr(rc, "reasoning_config_for_model", lambda _: shape)
    assert rc.effective_reasoning_effort(None, "managed-qwen") == "medium"
    assert rc.effective_reasoning_effort("low", "managed-qwen") == "low"


def test_managed_omlx_never_applies_medium_when_the_model_does_not_support_it():
    shape = reasoning_shape_from_discovery(
        {
            "reasoning_efforts": ["low", "xhigh"],
            "reasoning_effort_default": "low",
        },
        default_effort_when_unset="medium",
    )

    assert shape == {
        "type": "effort_levels",
        "values": ["low", "xhigh"],
        "default": "low",
    }


def test_personal_omlx_keeps_an_unset_effort_unset(monkeypatch):
    from llm import reasoning_config as rc

    shape = reasoning_shape_from_discovery(
        {
            "reasoning_efforts": ["low", "medium", "xhigh"],
            "reasoning_effort_default": "low",
        }
    )

    assert shape == {
        "type": "effort_levels",
        "values": ["low", "medium", "xhigh"],
        "default": "low",
    }
    monkeypatch.setattr(rc, "reasoning_config_for_model", lambda _: shape)
    assert rc.effective_reasoning_effort(None, "personal-qwen") is None


def test_managed_omlx_configured_effort_wins_over_medium_default():
    shape = reasoning_shape_from_discovery(
        {
            "reasoning_efforts": ["low", "medium", "xhigh"],
            "reasoning_effort_default": "low",
            "configured_reasoning_effort": "xhigh",
        },
        default_effort_when_unset="medium",
    )

    assert shape["default"] == "xhigh"
    assert shape["applies_default_when_unset"] is True
