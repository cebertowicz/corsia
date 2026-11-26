"""Only site origins are journalled, and pending clears survive a restart."""
import json

import pytest

from host_access import browser_grant_records as records


@pytest.fixture(autouse=True)
def clear_memory():
    records._armed.clear()
    records._pending.clear()
    records._generation.clear()
    records._transitions.clear()
    yield
    records._armed.clear()
    records._pending.clear()
    records._generation.clear()
    records._transitions.clear()


def test_timed_grant_is_armed_and_restart_makes_it_pending(tmp_path):
    profile = str(tmp_path / "profile")
    records.arm(profile, "https://login.example.com/path")
    assert records.pending(profile) == {}
    content = json.loads(records._path(profile).read_text())
    assert content == {"sites": ["https://login.example.com"]}
    records._armed.clear()
    assert "example.com" in records.pending(profile)
    assert records.complete(profile, "example.com", 0)
    assert not records._path(profile).exists()


def test_end_one_of_two_grants_remains_armed_after_clear(tmp_path):
    profile = str(tmp_path / "profile")
    records.arm(profile, "https://login.example.com")
    records.arm(profile, "https://login.example.com")
    generation = records.end(profile, "https://login.example.com")
    assert "example.com" in records.pending(profile)
    assert records.complete(profile, "example.com", generation)
    assert records.pending(profile) == {}
    assert records._path(profile).exists()


def test_new_end_prevents_old_clear_from_removing_record(tmp_path):
    profile = str(tmp_path / "profile")
    first = records.end(profile, "https://example.com")
    second = records.end(profile, "https://example.com")
    assert not records.complete(profile, "example.com", first)
    assert records.complete(profile, "example.com", second)


def test_clearing_one_site_preserves_another_pending_site(tmp_path):
    profile = str(tmp_path / "profile")
    first = records.end(profile, "https://example.com")
    records.end(profile, "https://other.org")
    assert records.complete(profile, "example.com", first)
    assert set(records.pending(profile)) == {"other.org"}
