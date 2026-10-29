"""Measure the setup poll and source projection without live processes or app startup."""

from pathlib import Path
from types import SimpleNamespace

from fastapi import FastAPI
from fastapi.testclient import TestClient


def test_setup_poll_and_source_read_budget(isolated_db, monkeypatch, tmp_path, capsys):
    from api import setup
    from infra import database
    from infra.default_port import DEFAULT_BACKEND_PORT
    from infra import setup_progress
    from llm import local_setup, local_setup_prepare, local_sources, network_discovery
    from llm.managed_omlx_binary import MANAGED_INSTANCE_ID, managed_omlx_paths

    monkeypatch.setattr("infra.config.STATE_ROOT", tmp_path)
    monkeypatch.setattr(local_setup, "_flight", local_setup._Flight())
    monkeypatch.setattr(local_setup_prepare, "local_memory_ok", lambda: True)
    monkeypatch.setattr(local_setup_prepare, "_prepare", local_setup_prepare._Prepare())
    with database.get_db() as db:
        db.execute("DELETE FROM users")
        db.commit()
    app = FastAPI()
    app.include_router(setup.router)
    app.dependency_overrides[setup.require_user] = lambda: {}
    with database.get_db() as db:
        setup_progress.start_after_account("app", db=db)  # an account was just made: setup is unfinished and resumable
        db.commit()
    base_url = f"http://127.0.0.1:{DEFAULT_BACKEND_PORT}"
    client = TestClient(app, base_url=base_url, client=("127.0.0.1", 50100))
    paths = managed_omlx_paths()
    counts = {"subprocesses": 0, "file_reads": 0, "queries": 0}
    original_open = Path.open
    original_make = database._make_connection

    def trace(statement):
        if statement.lstrip().upper().startswith("SELECT"):
            counts["queries"] += 1

    def make(path):
        connection = original_make(path)
        connection.set_trace_callback(trace)
        return connection

    def opened(path, mode="r", *args, **kwargs):
        if "r" in mode:
            counts["file_reads"] += 1
        return original_open(path, mode, *args, **kwargs)

    def no_process(*_a, **_kw):
        counts["subprocesses"] += 1
        raise AssertionError("a setup read must not start a subprocess")

    monkeypatch.setattr(database, "_make_connection", make)
    for connection in database._all_connections:
        connection.set_trace_callback(trace)
    monkeypatch.setattr(Path, "open", opened)
    monkeypatch.setattr("subprocess.Popen", no_process)
    try:
        measurements = []
        progress_over_status = []
        for scenario in ("empty", "managed_offline", "managed_online", "active_flight"):
            if scenario == "managed_offline":
                server = network_discovery.upsert_server(
                    "omlx", MANAGED_INSTANCE_ID, "This Mac", "127.0.0.1", 8210,
                    "offline", kind="managed", source="corsia",
                )
                network_discovery.set_server_metadata(server, corsia_managed=True,
                    base_path=str(paths.base_path), model_dir=str(paths.model_dir))
            elif scenario == "managed_online":
                network_discovery.update_server_status(server, status="online")
            elif scenario == "active_flight":
                local_setup._flight.task = SimpleNamespace(done=lambda: False)
                local_setup._flight.stage = "Installing"
            for repetition in ("cold", "warm"):
                counts.update(subprocesses=0, file_reads=0, queries=0)
                response = client.get("/api/setup/local-download")
                assert response.status_code == 200
                measurements.append((scenario, repetition, "GET local-download", counts.copy()))
                counts.update(subprocesses=0, file_reads=0, queries=0)
                local_sources._omlx_state()
                measurements.append((scenario, repetition, "_omlx_state", counts.copy()))
                assert counts["subprocesses"] == 0
                counts.update(subprocesses=0, file_reads=0, queries=0)
                assert client.get("/api/setup/status").status_code == 200
                status_reads = counts.copy()
                measurements.append((scenario, repetition, "GET status", status_reads))
                counts.update(subprocesses=0, file_reads=0, queries=0)
                assert "local_started" in client.get("/api/setup/progress").json()["status"]
                progress_reads = counts.copy()
                measurements.append((scenario, repetition, "GET progress", progress_reads))
                progress_over_status.append({key: progress_reads[key] - status_reads[key] for key in counts})
        with capsys.disabled():
            for row in measurements:
                print("SETUP_READ_BUDGET", row)
        assert all(row[3]["subprocesses"] == 0 for row in measurements)
        # The signed-in progress read costs the status read's queries plus exactly the two local-setup record
        # file reads (measured: +2 file reads, +0 queries, in every scenario); the pre-auth status read costs none.
        assert all(extra == {"subprocesses": 0, "file_reads": 2, "queries": 0} for extra in progress_over_status)
    finally:
        for connection in database._all_connections:
            connection.set_trace_callback(None)
