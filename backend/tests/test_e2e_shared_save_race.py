"""The E2E gate pauses only the armed shared path and releases the real staging call."""
from concurrent.futures import ThreadPoolExecutor

import pytest
from fastapi import HTTPException


def test_gate_waits_after_staging_and_releases(tmp_path, monkeypatch):
    import e2e_shared_save_race as race
    from registry import durable_writes

    shared = tmp_path / 'shared' / 'documents'
    shared.mkdir(parents=True)
    target = shared / 'one.md'
    target.write_text('before')
    monkeypatch.setattr(race.config, 'WORKSPACE', tmp_path)
    monkeypatch.setenv('CORSIA_TEST_MODE', '1')
    monkeypatch.setenv('CORSIA_E2E_MOCK', '1')
    monkeypatch.setenv('CORSIA_WORKSPACE', str(tmp_path))
    monkeypatch.setenv('CORSIA_STATE_DIR', str(tmp_path / 'state'))
    calls = []
    monkeypatch.setattr(durable_writes, 'stage_bound_bytes', lambda path, *args, **kwargs: calls.append(path))
    race.arm(race.Arm(path=str(target)))
    try:
        with race.install_gate(), ThreadPoolExecutor(max_workers=1) as executor:
            staged = executor.submit(durable_writes.stage_bound_bytes, target, b'content')
            assert race._gates[target].arrived.wait(5)
            assert calls == [target]
            assert not staged.done()
            target.write_text('outside')
            race.release(race.Arm(path=str(target)))
            staged.result(timeout=5)
        assert target.read_text() == 'outside'
    finally:
        race._gates.pop(target, None)


def test_gate_rejects_paths_outside_shared(tmp_path, monkeypatch):
    import e2e_shared_save_race as race

    target = tmp_path / 'other.md'
    target.write_text('before')
    monkeypatch.setattr(race.config, 'WORKSPACE', tmp_path)
    monkeypatch.setenv('CORSIA_TEST_MODE', '1')
    monkeypatch.setenv('CORSIA_E2E_MOCK', '1')
    monkeypatch.setenv('CORSIA_WORKSPACE', str(tmp_path))
    monkeypatch.setenv('CORSIA_STATE_DIR', str(tmp_path / 'state'))
    with pytest.raises(HTTPException) as exc:
        race.arm(race.Arm(path=str(target)))
    assert exc.value.status_code == 400
