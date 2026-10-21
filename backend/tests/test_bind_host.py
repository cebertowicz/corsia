"""The setup choice and the one bind-host reader (onboarding 1a, #3286)."""

from __future__ import annotations

import argparse
import json
import os
import sqlite3
import subprocess
import sys
from contextlib import closing
from pathlib import Path

import pytest

from infra.bind_host import BindHostError, resolve_bind_host, write_setup
from infra.platform_paths import load_bootstrap, runtime_db_paths

BACKEND = Path(__file__).resolve().parents[1]


def _bootstrap(tmp_path: Path, **keys) -> Path:
    path = tmp_path / "config.json"
    path.write_text(json.dumps({"version": 2, "state_root": "/s", **keys}))
    return path


class TestReader:
    def test_missing_bootstrap_keeps_the_legacy_bind(self, tmp_path):
        assert resolve_bind_host(tmp_path / "absent.json") == "0.0.0.0"

    def test_existing_legacy_bootstrap_is_migrated_to_explicit_server_exposure(self, tmp_path):
        path = _bootstrap(tmp_path)
        assert resolve_bind_host(path) == "0.0.0.0"
        assert json.loads(path.read_text()) == {
            "version": 2,
            "state_root": "/s",
            "setup": "server",
            "bind_host": "0.0.0.0",
        }

    @pytest.mark.parametrize(
        ("setup", "bind"), [("local", "127.0.0.1"), ("server", "0.0.0.0")]
    )
    def test_recorded_setup_chooses_the_bind(self, tmp_path, setup, bind):
        path = _bootstrap(tmp_path, setup=setup, bind_host=bind)
        assert resolve_bind_host(path) == bind

    @pytest.mark.parametrize(
        "keys",
        [
            {"setup": "cloud", "bind_host": "0.0.0.0"},
            {"setup": "local", "bind_host": "10.0.0.5"},
            {"setup": "local"},
            {"bind_host": "127.0.0.1"},
        ],
    )
    def test_unknown_or_partial_values_refuse_with_the_fix(self, tmp_path, keys):
        with pytest.raises(BindHostError, match="corsia setup local"):
            resolve_bind_host(_bootstrap(tmp_path, **keys))

    @pytest.mark.parametrize(
        ("setup", "bind"), [("local", "0.0.0.0"), ("server", "127.0.0.1")]
    )
    def test_setup_bind_mismatch_refuses(self, tmp_path, setup, bind):
        with pytest.raises(BindHostError, match="does not match"):
            resolve_bind_host(_bootstrap(tmp_path, setup=setup, bind_host=bind))

    def test_invalid_json_refuses(self, tmp_path):
        path = tmp_path / "config.json"
        path.write_text("{not json")
        with pytest.raises(BindHostError, match="not valid JSON"):
            resolve_bind_host(path)

    def test_write_setup_keeps_every_other_key(self, tmp_path):
        path = _bootstrap(tmp_path, workspace={"id": "w"})
        write_setup(path, "local")
        payload = json.loads(path.read_text())
        assert payload["workspace"] == {"id": "w"} and payload["state_root"] == "/s"
        assert (payload["setup"], payload["bind_host"]) == ("local", "127.0.0.1")
        write_setup(path, "server")
        assert resolve_bind_host(path) == "0.0.0.0"


def _cli(*args: str, home: Path, env: dict[str, str] | None = None):
    """Run ``python -m cli`` as a fresh machine would: no workspace env at all."""
    clean = {
        key: value
        for key, value in os.environ.items()
        if key not in {"CORSIA_WORKSPACE", "CORSIA_STATE_DIR"}
    }
    clean.update(HOME=str(home), CORSIA_ANALYTICS_DISABLE="1", **(env or {}))
    return subprocess.run(
        [sys.executable, "-m", "cli", *args],
        cwd=BACKEND,
        env=clean,
        text=True,
        capture_output=True,
        check=False,
    )


class TestCommands:
    def test_bind_host_prints_or_refuses_with_status(self, tmp_path):
        good = _bootstrap(tmp_path, setup="local", bind_host="127.0.0.1")
        result = _cli("bind-host", "--bootstrap", str(good), home=tmp_path)
        assert (result.returncode, result.stdout) == (0, "127.0.0.1\n")
        bad = tmp_path / "bad.json"
        bad.write_text(json.dumps({"setup": "local", "bind_host": "0.0.0.0"}))
        result = _cli("bind-host", "--bootstrap", str(bad), home=tmp_path)
        assert result.returncode == 2 and result.stdout == ""
        assert "corsia setup local" in result.stderr

    def test_fresh_machine_init_with_flags_writes_setup_and_bind(self, tmp_path):
        # No bootstrap and no CORSIA_WORKSPACE: the state a new install starts in.
        result = _cli(
            "init", "--workspace", str(tmp_path / "work"), "--setup", "server",
            home=tmp_path,
        )
        assert result.returncode == 0, result.stderr
        payload = json.loads((tmp_path / ".corsia" / "config.json").read_text())
        assert (payload["setup"], payload["bind_host"]) == ("server", "0.0.0.0")
        assert payload["workspace"]["root"] == str((tmp_path / "work").resolve())
        assert _cli("bind-host", home=tmp_path).stdout == "0.0.0.0\n"

    def test_fresh_machine_init_defaults_to_corsia_and_local(self, tmp_path):
        result = subprocess.run(
            [sys.executable, "-m", "cli", "init"],
            cwd=BACKEND,
            env={**{k: v for k, v in os.environ.items()
                    if k not in {"CORSIA_WORKSPACE", "CORSIA_STATE_DIR"}},
                 "HOME": str(tmp_path), "CORSIA_ANALYTICS_DISABLE": "1"},
            input="\n\n",
            text=True,
            capture_output=True,
            check=False,
        )
        assert result.returncode == 0, result.stderr
        assert "On this computer only" in result.stdout
        payload = json.loads((tmp_path / ".corsia" / "config.json").read_text())
        assert payload["workspace"]["root"] == str((tmp_path / "corsia").resolve())
        assert (payload["setup"], payload["bind_host"]) == ("local", "127.0.0.1")

    def test_setup_command_switches_and_requires_a_bootstrap(self, tmp_path):
        result = _cli("setup", "local", home=tmp_path)
        assert result.returncode == 2 and "corsia start" in result.stderr
        (tmp_path / ".corsia").mkdir()
        _bootstrap(tmp_path / ".corsia")
        assert _cli("setup", "local", home=tmp_path).returncode == 0
        assert _cli("bind-host", home=tmp_path).stdout == "127.0.0.1\n"

    def test_remote_address_prints_only_a_saved_address_while_devices_can_connect(self, tmp_path):
        from infra.settings import DEVICE_SETTING_KEY

        assert _cli("init", "--workspace", str(tmp_path / "work"), "--setup", "local",
                    home=tmp_path).returncode == 0
        db_path = runtime_db_paths(load_bootstrap(tmp_path / ".corsia" / "config.json").state_root)[0]
        assert _cli("remote-address", home=tmp_path).stdout == ""
        assert not db_path.exists()  # read-only: never creates the database

        def save(value: dict) -> None:
            with closing(sqlite3.connect(db_path)) as db, db:
                db.execute("CREATE TABLE IF NOT EXISTS settings (key TEXT PRIMARY KEY, value TEXT)")
                db.execute("INSERT OR REPLACE INTO settings VALUES (?, ?)",
                           (DEVICE_SETTING_KEY, json.dumps(value)))

        db_path.parent.mkdir(parents=True, exist_ok=True)
        save({"enabled": False, "outside_address": "home.example.com"})
        assert _cli("remote-address", home=tmp_path).stdout == ""
        save({"enabled": True})
        assert _cli("remote-address", home=tmp_path).stdout == ""
        save({"enabled": True, "outside_address": "home.example.com"})
        result = _cli("remote-address", home=tmp_path)
        assert (result.returncode, result.stdout) == (0, "home.example.com\n")

        # The app passes its own state root, so a checkout install's address never shows for it.
        app_root = tmp_path / "app-state"
        assert _cli("remote-address", "--state-root", str(app_root), home=tmp_path).stdout == ""
        app_root.mkdir()
        with closing(sqlite3.connect(app_root / "corsia.db")) as db, db:
            db.execute("CREATE TABLE settings (key TEXT PRIMARY KEY, value TEXT)")
            db.execute("INSERT INTO settings VALUES (?, ?)",
                       (DEVICE_SETTING_KEY, json.dumps({"enabled": True, "outside_address": "app.example.com"})))
        app = _cli("remote-address", "--state-root", str(app_root), home=tmp_path)
        assert (app.returncode, app.stdout) == (0, "app.example.com\n")


def test_init_force_keeps_the_recorded_setup_as_the_default(tmp_path, monkeypatch):
    import cli

    bootstrap = tmp_path / "config.json"
    monkeypatch.setattr(cli, "BOOTSTRAP_PATH", bootstrap)
    monkeypatch.setenv("CORSIA_STATE_DIR", str(tmp_path / "state"))
    workspace = tmp_path / "workspace"
    args = argparse.Namespace(force=False, path=str(workspace), name=None, setup="server")
    assert cli.cmd_init(args) == 0
    prompts: list[str] = []
    monkeypatch.setattr("builtins.input", lambda prompt: prompts.append(prompt) or "")
    args = argparse.Namespace(force=True, path=str(workspace), name=None, setup=None)
    assert cli.cmd_init(args) == 0
    assert prompts == ["     Choose 1 or 2 (press Enter for 2): "]
    assert resolve_bind_host(bootstrap) == "0.0.0.0"


class TestOmlxFollowsTheBind:
    @pytest.fixture
    def bootstrap(self, tmp_path, monkeypatch):
        path = tmp_path / "config.json"
        monkeypatch.setattr("infra.config.BOOTSTRAP_CONFIG_PATH", path)
        return path

    def test_local_setup_keeps_omlx_on_loopback(self, bootstrap):
        from llm.providers.omlx_supervisor import _listen_host

        bootstrap.write_text(json.dumps({"setup": "local", "bind_host": "127.0.0.1"}))
        assert _listen_host("192.168.1.20") == "127.0.0.1"

    @pytest.mark.parametrize(
        "payload",
        [{"setup": "server", "bind_host": "0.0.0.0"}, {"version": 2}],
    )
    def test_server_and_legacy_keep_the_endpoint_host(self, bootstrap, payload):
        from llm.providers.omlx_supervisor import _listen_host

        bootstrap.write_text(json.dumps(payload))
        assert _listen_host("192.168.1.20") == "192.168.1.20"

    def test_unreadable_choice_falls_back_to_loopback(self, bootstrap):
        from llm.providers.omlx_supervisor import _listen_host

        bootstrap.write_text(json.dumps({"setup": "local", "bind_host": "0.0.0.0"}))
        assert _listen_host("192.168.1.20") == "127.0.0.1"

    async def test_start_server_spawns_omlx_on_the_listen_host(self, bootstrap, monkeypatch):
        from llm.providers import omlx_supervisor as sup

        bootstrap.write_text(json.dumps({"setup": "local", "bind_host": "127.0.0.1"}))
        spawned: list[list[str]] = []

        async def fake_run_in_db(fn, *args):
            return fn(*args)

        monkeypatch.setattr(sup, "run_in_db", fake_run_in_db)
        monkeypatch.setattr(sup, "_read_pid", lambda sid, **_kwargs: None)
        monkeypatch.setattr(sup, "_port_owner_pid", lambda host, port: None)
        monkeypatch.setattr(sup.shutil, "which", lambda name: "/opt/homebrew/bin/omlx")
        monkeypatch.setattr(sup, "_resolve_settings", lambda cfg: {})
        monkeypatch.setattr(
            sup, "_spawn_process", lambda argv, log: spawned.append(argv) or {"ok": False}
        )
        result = await sup.start_server(
            {"server_id": "s1", "host": "192.168.1.20", "port": 8080}
        )
        assert result == {"ok": False}
        assert spawned[0][spawned[0].index("--host") + 1] == "127.0.0.1"
