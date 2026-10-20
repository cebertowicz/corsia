from __future__ import annotations

from copy import deepcopy

import pytest

from library import index


def _index_payload(*, withdrawn: bool = False) -> dict:
    return {
        "skills": [{
            "name": "sample",
            "description": "A sample skill.",
            "version": "1.0.0",
            "license": "MIT",
            "source": {
                "repository": "cebertowicz/corsia-library",
                "path": "skills/sample",
                "commit": "0123456789abcdef" * 2 + "01234567",
            },
            "files": [{"path": "SKILL.md", "sha256": "sha256:" + "a" * 64}],
            "withdrawn": withdrawn,
            "withdrawn_reason": "Superseded by sample-v2" if withdrawn else None,
        }],
    }


def _seams(monkeypatch, tmp_path, *, now: float = 1_000.0) -> list[float]:
    clock = [now]
    monkeypatch.setattr(index, "_state_root", lambda: tmp_path / "state")
    monkeypatch.setattr(index.time, "time", lambda: clock[0])
    return clock


def test_first_raw_fetch_without_a_cache_is_refreshed(monkeypatch, tmp_path):
    _seams(monkeypatch, tmp_path)
    payload = _index_payload()
    monkeypatch.setattr(index, "_fetch_index", lambda: payload)

    result = index.load_index()

    assert result.skills == (payload["skills"][0],)
    assert result.index_age == "just now"
    assert (tmp_path / "state" / "library" / "index.json").exists()
    assert result.refreshed is True
    assert result.generations == {"sample": 1}


def test_unchanged_second_raw_fetch_is_not_refreshed(monkeypatch, tmp_path):
    _seams(monkeypatch, tmp_path)
    payload = _index_payload()
    calls = []
    monkeypatch.setattr(index, "_fetch_index", lambda: calls.append(1) or payload)

    index.load_index()
    unchanged = index.load_index()

    assert calls == [1, 1]
    assert unchanged.refreshed is False
    assert unchanged.generations == {"sample": 1}


def test_changed_raw_fetch_is_refreshed(monkeypatch, tmp_path):
    _seams(monkeypatch, tmp_path)
    initial = _index_payload()
    changed = deepcopy(initial)
    changed["skills"][0]["version"] = "1.1.0"
    payloads = iter((initial, changed))
    monkeypatch.setattr(index, "_fetch_index", lambda: next(payloads))

    index.load_index()
    refreshed = index.load_index()

    assert refreshed.skills == (changed["skills"][0],)
    assert refreshed.refreshed is True
    assert refreshed.generations == {"sample": 2}


def test_unrelated_index_changes_preserve_an_item_generation(monkeypatch, tmp_path):
    _seams(monkeypatch, tmp_path)
    initial = _index_payload()
    changed = deepcopy(initial)
    other = deepcopy(changed["skills"][0])
    other["name"] = "other"
    changed["skills"].append(other)
    payloads = iter((initial, changed))
    monkeypatch.setattr(index, "_fetch_index", lambda: next(payloads))

    index.load_index()
    refreshed = index.load_index()

    assert refreshed.generations == {"sample": 1, "other": 1}


def test_stale_cache_is_used_when_raw_fetch_fails(monkeypatch, tmp_path):
    clock = _seams(monkeypatch, tmp_path)
    payload = _index_payload()
    monkeypatch.setattr(index, "_fetch_index", lambda: payload)
    index.load_index()
    clock[0] += 7_200
    monkeypatch.setattr(index, "_fetch_index", lambda: (_ for _ in ()).throw(OSError("offline")))

    result = index.load_index()

    assert result.skills == (payload["skills"][0],)
    assert result.index_age == "2 hours ago"


def test_no_cache_failure_is_loud_and_machine_readable(monkeypatch, tmp_path):
    _seams(monkeypatch, tmp_path)
    monkeypatch.setattr(index, "_fetch_index", lambda: (_ for _ in ()).throw(OSError("offline")))

    with pytest.raises(index.LibraryUnreachableError, match="library_unreachable"):
        index.load_index()


def test_withdrawn_row_is_valid_and_preserved(monkeypatch, tmp_path):
    _seams(monkeypatch, tmp_path)
    payload = _index_payload(withdrawn=True)
    monkeypatch.setattr(index, "_fetch_index", lambda: payload)

    result = index.load_index()

    assert result.skills[0]["withdrawn"] is True
    assert result.skills[0]["withdrawn_reason"] == "Superseded by sample-v2"


@pytest.mark.parametrize(
    ("mutate", "message"),
    [
        (lambda payload: payload["skills"][0]["files"][0].update(path="../escape"), "unsafe relative path"),
        (lambda payload: payload["skills"][0]["source"].update(path="/absolute"), "unsafe relative path"),
        (lambda payload: payload["skills"][0]["files"][0].update(sha256="not-a-hash"), "sha256"),
        (lambda payload: payload["skills"][0]["source"].update(commit="short"), "40-character SHA"),
        (lambda payload: payload["skills"][0].update(files=[]), "include SKILL.md"),
        (lambda payload: payload["skills"][0]["files"].append({"path": "skill.md", "sha256": "a" * 64}), "unique, ignoring case"),
        (lambda payload: payload["skills"].append(deepcopy(payload["skills"][0])), "skill names must be unique"),
    ],
)
def test_rejects_unsafe_paths_and_hashes(monkeypatch, tmp_path, mutate, message):
    _seams(monkeypatch, tmp_path)
    payload = deepcopy(_index_payload())
    mutate(payload)
    monkeypatch.setattr(index, "_fetch_index", lambda: payload)

    with pytest.raises(index.LibraryIndexValidationError, match=message):
        index.load_index()
