from __future__ import annotations

import pytest

from core.device_pairing import (
    CODE_ALPHABET,
    CODE_TTL_SECONDS,
    DevicePairing,
    PairingError,
    canonical_verified_host,
    transcript_prefix,
)


def test_code_uses_unambiguous_four_character_alphabet():
    pairing = DevicePairing()
    pairing.enable()

    code = pairing.issue_code()

    assert len(code) == 4
    assert set(code) <= set(CODE_ALPHABET)
    assert pairing.status() == {"state": "active", "expires_at": pytest.approx(pairing.status()["expires_at"])}


def test_disable_ends_code_and_new_code_replaces_previous_generation():
    pairing = DevicePairing()
    pairing.enable()
    first = pairing.issue_code()
    second = pairing.issue_code()

    assert first != second or len(CODE_ALPHABET) ** 4 > 1
    pairing.disable()
    assert pairing.status() == {"state": "none", "expires_at": None}


def test_expired_code_is_not_active():
    now = [0.0]
    pairing = DevicePairing(clock=lambda: now[0])
    pairing.enable()
    pairing.issue_code()
    now[0] = CODE_TTL_SECONDS

    assert pairing.status() == {"state": "none", "expires_at": None}
    with pytest.raises(PairingError, match="expired"):
        pairing.start(msg_c=b"client", transcript_prefix=b"test")


def test_verified_host_canonicalizes_case_and_trailing_dot():
    assert canonical_verified_host("Studio.Local.") == "studio.local"
    with pytest.raises(ValueError, match="port"):
        canonical_verified_host("studio.local:47901")


def test_pinned_and_verified_transcripts_are_distinct():
    spki = bytes(32)
    assert transcript_prefix("verified", host="studio.local") != transcript_prefix(
        "pinned", spki_sha256=spki
    )


def _record_wrong_tries(pairing: DevicePairing, count: int = 5) -> None:
    for _ in range(count):
        pairing._record_failure()


def test_fifth_failure_ends_code():
    pairing = DevicePairing()
    pairing.enable()
    pairing.issue_code()

    _record_wrong_tries(pairing)

    assert pairing.status()["state"] == "none"


def test_hourly_failures_carry_across_new_code():
    pairing = DevicePairing()
    pairing.enable()

    for code_number in range(4):
        pairing.issue_code()
        _record_wrong_tries(pairing)
        expected = "paused" if code_number == 3 else "none"
        assert pairing.status()["state"] == expected


def test_new_code_from_paused_clears_window():
    pairing = DevicePairing()
    pairing.enable()
    for _ in range(4):
        pairing.issue_code()
        _record_wrong_tries(pairing)
    assert pairing.status()["state"] == "paused"

    pairing.issue_code()
    _record_wrong_tries(pairing)

    assert pairing.status()["state"] == "none"


def test_failures_age_out_after_an_hour():
    now = [0.0]
    pairing = DevicePairing(clock=lambda: now[0])
    pairing.enable()
    pairing.issue_code()
    _record_wrong_tries(pairing)

    now[0] = 3599.0
    pairing.issue_code()
    _record_wrong_tries(pairing)

    now[0] = 3600.0
    pairing.issue_code()
    _record_wrong_tries(pairing)

    assert pairing.status()["state"] == "none"
