from __future__ import annotations

import resource

import pytest

from infra.file_descriptors import (
    DESIRED_FILE_DESCRIPTOR_LIMIT,
    raise_file_descriptor_limit,
)


def _patch_limits(monkeypatch, *, soft: int, hard: int) -> list[tuple[int, int]]:
    applied: list[tuple[int, int]] = []

    def fake_get(_which):
        return (soft, hard)

    def fake_set(_which, limits):
        applied.append(limits)

    monkeypatch.setattr(resource, "getrlimit", fake_get)
    monkeypatch.setattr(resource, "setrlimit", fake_set)
    return applied


def test_the_macos_default_is_raised(monkeypatch):
    """256 descriptors is the condition that took every lane down at once."""
    applied = _patch_limits(monkeypatch, soft=256, hard=resource.RLIM_INFINITY)

    assert raise_file_descriptor_limit() == DESIRED_FILE_DESCRIPTOR_LIMIT
    assert applied == [(DESIRED_FILE_DESCRIPTOR_LIMIT, resource.RLIM_INFINITY)]


def test_a_finite_hard_limit_caps_the_request(monkeypatch):
    """Ask for no more than the kernel will grant, so the call succeeds."""
    applied = _patch_limits(monkeypatch, soft=256, hard=4096)

    assert raise_file_descriptor_limit() == 4096
    assert applied == [(4096, 4096)]


def test_an_already_generous_limit_is_left_alone(monkeypatch):
    """Never lower a limit a host or operator deliberately set higher."""
    applied = _patch_limits(
        monkeypatch,
        soft=DESIRED_FILE_DESCRIPTOR_LIMIT * 2,
        hard=resource.RLIM_INFINITY,
    )

    assert raise_file_descriptor_limit() == DESIRED_FILE_DESCRIPTOR_LIMIT * 2
    assert applied == []


def test_an_unraisable_limit_is_reported_not_hidden(monkeypatch):
    """A hard limit at the default leaves the risk on the record."""
    applied = _patch_limits(monkeypatch, soft=256, hard=256)

    assert raise_file_descriptor_limit() == 256
    assert applied == []


@pytest.mark.parametrize("failure", [ValueError("refused"), OSError("refused")])
def test_a_refused_raise_does_not_stop_startup(monkeypatch, failure):
    """Losing the raise is bad; failing to boot because of it is worse."""
    monkeypatch.setattr(
        resource, "getrlimit", lambda _which: (256, resource.RLIM_INFINITY)
    )

    def refuse(_which, _limits):
        raise failure

    monkeypatch.setattr(resource, "setrlimit", refuse)

    assert raise_file_descriptor_limit() == 256


def test_the_real_process_limit_clears_the_macos_default():
    """Exercise the real syscall, not just the mocked contract."""
    original = resource.getrlimit(resource.RLIMIT_NOFILE)
    try:
        assert raise_file_descriptor_limit() >= min(
            DESIRED_FILE_DESCRIPTOR_LIMIT, original[1]
        ) or original[0] >= DESIRED_FILE_DESCRIPTOR_LIMIT
        assert resource.getrlimit(resource.RLIMIT_NOFILE)[0] > 256
    finally:
        resource.setrlimit(resource.RLIMIT_NOFILE, original)
