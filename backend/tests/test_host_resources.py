
from __future__ import annotations

import os
import sys
from unittest.mock import patch

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))

from infra.host_resources import (  # noqa: E402
    LOCAL_PROVIDER_TYPES, MIN_IMAGE_DECODE_PIXELS, SAFE_RESERVE_BYTES,
    _resolved_total_ram_bytes, check_can_load, get_host_resources,
    host_total_ram_bytes, image_decode_pixel_budget, load_reserve_bytes,
    log_image_decode_budget, max_image_decode_pixels, model_room_bytes,
)

GIB = 1024 ** 3
_SMALL_HOST = 16 * GIB
_LARGE_HOST = 274877906944  # the 256 GB workstation this is measured against
_SLOTS = 2

# Declared frames the delivery path actually meets, in pixels.
_PHONE_12MP = 4032 * 3024
_PHONE_48MP = 8000 * 6000
_CAMERA_50MP = 8160 * 6144
_PATHOLOGICAL_108MP = 12000 * 9000
_PATHOLOGICAL_169MP = 13000 * 13000



def _vmstub(total_gb=256, available_gb=128):
    GIB = 1024 ** 3
    class _VM:
        total = int(total_gb * GIB)
        available = int(available_gb * GIB)
    return _VM()



@pytest.mark.asyncio
async def test_host_resources_shape():
    fake_loaded = {"omlx": [{"model_id": "x", "memory_gb": 10}], "lmstudio": []}
    with patch("psutil.virtual_memory", return_value=_vmstub(256, 128)):
        with patch("infra.host_resources._gather_local_loaded", return_value=fake_loaded):
            res = await get_host_resources()
    assert res["total_gb"] == 256
    assert res["free_gb"] == 128
    assert res["models_used_gb"] == 10  # only the omlx model contributes
    assert res["other_used_gb"] == round((256 - 128) - 10, 2)
    assert "omlx" in res["by_provider"]
    assert res["by_provider"]["omlx"]["used_gb"] == 10
    assert res["by_provider"]["omlx"]["models"][0]["model_id"] == "x"


@pytest.mark.asyncio
async def test_host_resources_safe_free_caps_at_zero():
    fake_loaded = {"omlx": [], "lmstudio": [], "llama": []}
    with patch("psutil.virtual_memory", return_value=_vmstub(64, 8)):
        with patch("infra.host_resources._gather_local_loaded", return_value=fake_loaded):
            res = await get_host_resources()
    assert res["free_gb"] == 8
    assert res["safe_free_gb"] == 0



@pytest.mark.asyncio
async def test_check_can_load_ok_with_plenty_of_room():
    fake_loaded = {"omlx": [], "lmstudio": [], "llama": []}
    with patch("psutil.virtual_memory", return_value=_vmstub(256, 200)):
        with patch("infra.host_resources._gather_local_loaded", return_value=fake_loaded):
            res = await check_can_load(estimated_gb=18, target_provider="omlx")
    assert res["ok"] is True
    assert res["warning"] is None
    assert res["remote"] is False
    assert res["after_load_gb"] == round(200 - 18, 2)


@pytest.mark.asyncio
async def test_check_can_load_warns_when_exceeds_safe_reserve():
    fake_loaded = {"omlx": [], "lmstudio": [], "llama": []}
    with patch("psutil.virtual_memory", return_value=_vmstub(64, 25)):
        with patch("infra.host_resources._gather_local_loaded", return_value=fake_loaded):
            res = await check_can_load(estimated_gb=20, target_provider="omlx")
    assert res["ok"] is False
    assert res["warning"] is not None
    assert res["warning"] == "Loading would leave 5.0 GB of memory free (below the 8 GB safe reserve)."
    assert res["after_load_gb"] == 5
    assert res["safe_reserve_gb"] == 8


@pytest.mark.asyncio
async def test_check_can_load_warns_when_load_exceeds_total_free():
    fake_loaded = {"omlx": [], "lmstudio": [], "llama": []}
    with patch("psutil.virtual_memory", return_value=_vmstub(64, 30)):
        with patch("infra.host_resources._gather_local_loaded", return_value=fake_loaded):
            res = await check_can_load(estimated_gb=100, target_provider="omlx")
    assert res["ok"] is False
    # One sentence with the numbers, in the app's own word (memory); the dialog's hint owns the swap/kill consequence.
    assert res["warning"] == "Loading would need 70.0 GB more memory than is free."


@pytest.mark.asyncio
async def test_check_can_load_remote_provider_short_circuits():
    res = await check_can_load(estimated_gb=10000, target_provider="anthropic")
    assert res["ok"] is True
    assert res["remote"] is True
    assert res["free_gb"] is None


@pytest.mark.asyncio
async def test_check_can_load_unknown_provider_short_circuits():
    res = await check_can_load(estimated_gb=10, target_provider="unknown")
    assert res["ok"] is True
    assert res["remote"] is True



@pytest.mark.asyncio
async def test_local_provider_types_defined():
    assert LOCAL_PROVIDER_TYPES == {"omlx"}


def test_safe_reserve_default_is_16gb():
    assert SAFE_RESERVE_BYTES == 16 * 1024 * 1024 * 1024


def test_the_load_reserve_is_an_eighth_of_ram_up_to_16gb():
    assert load_reserve_bytes(24 * GIB) == 3 * GIB
    assert load_reserve_bytes(64 * GIB) == 8 * GIB
    assert load_reserve_bytes(128 * GIB) == SAFE_RESERVE_BYTES
    assert load_reserve_bytes(256 * GIB) == SAFE_RESERVE_BYTES


def test_model_room_is_available_memory_less_the_reserve(monkeypatch):
    monkeypatch.setattr("infra.host_resources.host_total_ram_bytes", lambda: 24 * GIB)
    monkeypatch.setattr("infra.host_resources.host_available_ram_bytes", lambda: 12 * GIB)
    assert model_room_bytes() == 9 * GIB
    monkeypatch.setattr("infra.host_resources.host_available_ram_bytes", lambda: 2 * GIB)
    assert model_room_bytes() == 0


@pytest.mark.asyncio
async def test_the_preload_check_and_setup_pick_share_one_reserve(monkeypatch):
    fake_loaded = {"omlx": []}
    monkeypatch.setattr("infra.host_resources.host_total_ram_bytes", lambda: 24 * GIB)
    monkeypatch.setattr("infra.host_resources.host_available_ram_bytes", lambda: 12 * GIB)
    with patch("psutil.virtual_memory", return_value=_vmstub(24, 12)):
        with patch("infra.host_resources._gather_local_loaded", return_value=fake_loaded):
            res = await get_host_resources()
    assert res["safe_reserve_gb"] == 3
    assert res["safe_free_gb"] * GIB == model_room_bytes()


def test_a_workstation_never_refuses_an_image_a_laptop_must():
    """The allowance is a property of the machine, not one number for everyone."""
    small = max_image_decode_pixels(_SMALL_HOST, _SLOTS)
    large = max_image_decode_pixels(_LARGE_HOST, _SLOTS)

    assert large > small * 4


def test_a_large_host_admits_every_frame_this_delivery_path_meets():
    """A 256 GB machine should never see a decline for a real image."""
    allowed = max_image_decode_pixels(_LARGE_HOST, _SLOTS)

    for frame in (
        _PHONE_12MP,
        _PHONE_48MP,
        _CAMERA_50MP,
        _PATHOLOGICAL_108MP,
        _PATHOLOGICAL_169MP,
    ):
        assert frame <= allowed


def test_a_small_host_keeps_photographs_and_refuses_the_pathological_tail():
    """16 GB is where the bound has to bite, and where it must not overreach."""
    allowed = max_image_decode_pixels(_SMALL_HOST, _SLOTS)

    assert _PHONE_12MP <= allowed
    assert _PHONE_48MP <= allowed
    assert _CAMERA_50MP <= allowed
    assert _PATHOLOGICAL_108MP > allowed
    assert _PATHOLOGICAL_169MP > allowed


def test_the_allowance_never_falls_as_a_machine_grows():
    """Monotonic in RAM, so no upgrade can make a machine refuse more."""
    totals = [2 * GIB, 4 * GIB, 8 * GIB, _SMALL_HOST, 64 * GIB, _LARGE_HOST]
    allowances = [max_image_decode_pixels(total, _SLOTS) for total in totals]

    assert allowances == sorted(allowances)


def test_the_smallest_machine_still_opens_an_ordinary_photograph():
    """Refusing every photo serves the user worse than the memory risk does."""
    assert max_image_decode_pixels(1 * GIB, _SLOTS) == MIN_IMAGE_DECODE_PIXELS
    assert MIN_IMAGE_DECODE_PIXELS > _PHONE_12MP


def test_more_concurrent_decodes_divide_the_same_allowance():
    """Two decodes at once may each hold half of what one alone could."""
    one = max_image_decode_pixels(_LARGE_HOST, 1)
    two = max_image_decode_pixels(_LARGE_HOST, 2)

    assert two == pytest.approx(one / 2, rel=1e-6)


def test_a_nonsense_slot_count_cannot_widen_the_allowance():
    """A zero or negative slot count must not divide by nothing."""
    assert max_image_decode_pixels(_LARGE_HOST, 0) == max_image_decode_pixels(
        _LARGE_HOST, 1
    )


def _said_at_startup() -> str:
    """Return the single line startup logs about the decode budget."""
    from loguru import logger

    said: list[str] = []
    sink = logger.add(said.append, level="INFO")
    try:
        log_image_decode_budget(2)
    finally:
        logger.remove(sink)
    assert len(said) == 1
    return said[0]


def test_startup_states_the_budget_an_operator_needs_to_explain_a_decline():
    """A decline is unexplainable unless the machine said its budget out loud."""
    _resolved_total_ram_bytes.cache_clear()
    try:
        with patch("psutil.virtual_memory", return_value=_vmstub(16, 8)):
            line = _said_at_startup()
    finally:
        _resolved_total_ram_bytes.cache_clear()

    assert "16 GB total RAM" in line
    assert "63 megapixels" in line
    assert "2 concurrent decodes" in line


def test_an_unmeasurable_host_is_treated_as_the_smallest_supported_one():
    """A machine that will not say how much RAM it has must not be assumed rich."""
    _resolved_total_ram_bytes.cache_clear()
    try:
        with patch("psutil.virtual_memory", side_effect=OSError("no such sysctl")):
            assert image_decode_pixel_budget(2) == MIN_IMAGE_DECODE_PIXELS
            assert "unmeasured RAM" in _said_at_startup()
    finally:
        _resolved_total_ram_bytes.cache_clear()


def test_the_host_is_read_once_and_not_on_every_image():
    """Total RAM cannot change under a running process; reading it again can."""
    _resolved_total_ram_bytes.cache_clear()
    try:
        with patch(
            "psutil.virtual_memory", return_value=_vmstub(16, 8)
        ) as virtual_memory:
            first = image_decode_pixel_budget(2)
            assert image_decode_pixel_budget(2) == first
            assert virtual_memory.call_count == 1
    finally:
        _resolved_total_ram_bytes.cache_clear()


def test_total_ram_comes_from_the_one_host_ram_reader():
    """One truth-teller for host RAM, so a test can fake the machine."""
    with patch("psutil.virtual_memory", return_value=_vmstub(16, 8)):
        assert host_total_ram_bytes() == _SMALL_HOST
