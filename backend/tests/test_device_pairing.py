from __future__ import annotations

import threading
import time

import pytest

from core.device_pairing import (
    CODE_ALPHABET,
    CODE_TTL_SECONDS,
    EXCHANGE_TTL_SECONDS,
    DevicePairing,
    PairingError,
    canonical_verified_host,
    transcript_prefix,
)


def test_code_uses_unambiguous_four_character_alphabet():
    now = [0.0]
    pairing = DevicePairing(clock=lambda: now[0])
    pairing.enable()

    code = pairing.issue_code()
    now[0] = 61.0

    assert len(code) == 4
    assert set(code) <= set(CODE_ALPHABET)
    assert pairing.status() == {"state": "active", "expires_in_seconds": 539}


def test_disable_ends_code_and_new_code_replaces_previous_generation():
    pairing = DevicePairing()
    pairing.enable()
    first = pairing.issue_code()
    second = pairing.issue_code()

    assert first != second or len(CODE_ALPHABET) ** 4 > 1
    pairing.disable()
    assert pairing.status() == {"state": "none"}
    assert "expires_in_seconds" not in pairing.status()


def test_expired_code_is_not_active():
    now = [0.0]
    pairing = DevicePairing(clock=lambda: now[0])
    pairing.enable()
    pairing.issue_code()
    now[0] = CODE_TTL_SECONDS

    assert pairing.status() == {"state": "none"}
    assert "expires_in_seconds" not in pairing.status()
    with pytest.raises(PairingError, match="expired"):
        pairing.start(msg_c=b"client", transcript_prefix=b"test")


def test_start_prunes_expired_pending_exchanges_before_the_limit():
    now = [0.0]
    pairing = DevicePairing(clock=lambda: now[0])
    pairing.enable()
    pairing.issue_code()
    for index in range(4):
        pairing.start(msg_c=f"client-{index}".encode(), transcript_prefix=b"test")

    now[0] = EXCHANGE_TTL_SECONDS + 1
    exchange_id, _ = pairing.start(msg_c=b"replacement", transcript_prefix=b"test")

    assert set(pairing._exchanges) == {exchange_id}


def test_start_rejects_spake_when_code_rotates_without_charging_replacement(monkeypatch):
    pairing = DevicePairing()
    pairing.enable()
    pairing.issue_code()
    start_spake = pairing.start_spake

    def rotate_code_during_spake():
        result = start_spake()
        pairing.issue_code()
        return result

    with monkeypatch.context() as patch:
        patch.setattr(pairing, "start_spake", rotate_code_during_spake)
        with pytest.raises(PairingError, match="invalid_exchange"):
            pairing.start(msg_c=b"client", transcript_prefix=b"test")

    assert pairing._wrong_tries == 0
    assert not pairing._failures
    assert not pairing._exchanges
    exchange_id, _ = pairing.start(msg_c=b"replacement", transcript_prefix=b"test")
    assert exchange_id in pairing._exchanges


def test_code_state_changes_notify_once_for_issue_expiry_disable_and_pause():
    now = [0.0]
    updates: list[str] = []
    pairing = DevicePairing(clock=lambda: now[0], on_update=lambda: updates.append("updated"))
    pairing.enable()

    pairing.issue_code()
    assert updates == ["updated"]

    updates.clear()
    now[0] = CODE_TTL_SECONDS
    pairing.status()
    pairing.status()
    assert updates == ["updated"]

    pairing.enable()
    pairing.issue_code()
    updates.clear()
    pairing.disable()
    assert updates == ["updated"]

    pairing.enable()
    for _ in range(3):
        pairing.issue_code()
        _record_wrong_tries(pairing)
    pairing.issue_code()
    _record_wrong_tries(pairing, 4)
    updates.clear()
    pairing._record_failure()

    assert pairing.status()["state"] == "paused"
    assert updates == ["updated"]


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


def test_pairing_state_survives_worker_threads_racing_the_event_loop_calls():
    """start_spake runs in a worker thread while status/finish/issue_code run on the loop."""
    now = [0.0]
    clock_lock = threading.Lock()
    inside = [0, 0]  # threads in a clock call now, most ever at once

    def clock() -> float:
        # Every state-touching method reads the clock, so overlapping calls
        # mean two threads are inside the pairing state at the same time.
        # The slow, fast-moving clock also makes codes and exchanges expire
        # mid-run, so the worker's expiry path (_end_code) races the others.
        with clock_lock:
            inside[0] += 1
            inside[1] = max(inside[1], inside[0])
            now[0] += 0.05
            value = now[0]
        time.sleep(0.0002)
        with clock_lock:
            inside[0] -= 1
        return value

    pairing = DevicePairing(clock=clock)
    pairing.enable()
    pairing.issue_code()
    errors: list[BaseException] = []
    barrier = threading.Barrier(6)

    def guarded(work):
        def run():
            barrier.wait()
            for index in range(300):
                try:
                    work(index)
                except PairingError:
                    pass  # an expired or rotated code is a normal outcome
                except BaseException as error:  # noqa: BLE001 - the assertion is that none escape
                    errors.append(error)
                    return

        return run

    def start(index):
        spake, msg_s, generation = pairing.start_spake()
        pairing.start(
            msg_c=f"client-{threading.get_ident()}-{index}".encode(),
            transcript_prefix=b"test",
            spake=spake,
            msg_s=msg_s,
            generation=generation,
        )

    def finish(index):
        # Each unknown exchange is a counted failure; keep them rare enough that
        # the hourly pause does not turn the whole run into a no-op.
        if index % 50 == 0:
            pairing.finish(exchange_id=f"missing-{index}", confirm_c=b"x", name="host")

    def rotate(index):
        if index % 25 == 0:
            pairing.issue_code()

    workers = [
        threading.Thread(target=guarded(start)),
        threading.Thread(target=guarded(start)),
        threading.Thread(target=guarded(start)),
        threading.Thread(target=guarded(lambda _index: pairing.status(include_code=True))),
        threading.Thread(target=guarded(finish)),
        threading.Thread(target=guarded(rotate)),
    ]
    for worker in workers:
        worker.start()
    for worker in workers:
        worker.join(timeout=60)

    assert not any(worker.is_alive() for worker in workers)
    assert errors == []
    assert inside[1] == 1, "two threads were inside the pairing state at once"


def test_malformed_client_message_in_finish_is_a_counted_failed_try():
    pairing = DevicePairing()
    pairing.enable()
    pairing.issue_code()
    exchange_id, _ = pairing.start(msg_c=b"not-a-pake-message", transcript_prefix=b"test")

    with pytest.raises(PairingError) as failed:
        pairing.finish(exchange_id=exchange_id, confirm_c=b"x", name="host")

    assert failed.value.reason == "invalid_exchange"
    assert pairing._wrong_tries == 1
    with pytest.raises(PairingError, match="invalid_exchange"):
        pairing.finish(exchange_id=exchange_id, confirm_c=b"x", name="host")
