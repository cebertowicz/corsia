
from __future__ import annotations

import os
import sys
import json
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

_BACKEND_DIR = os.path.dirname(os.path.dirname(__file__))
if _BACKEND_DIR not in sys.path:
    sys.path.insert(0, _BACKEND_DIR)



class TestCleanCodexEnv:

    def _clean(self, env: dict) -> dict:
        with patch.dict(os.environ, env, clear=True):
            from subagents.codex_env import clean_codex_env
            return clean_codex_env()


    def test_strips_openai_api_key(self):
        result = self._clean({"OPENAI_API_KEY": "sk-test-123", "HOME": "/home/test"})
        assert "OPENAI_API_KEY" not in result, (
            "OPENAI_API_KEY must NEVER reach the codex subprocess"
        )

    def test_strips_openai_org_id(self):
        result = self._clean({"OPENAI_ORG_ID": "org-abc", "PATH": "/usr/bin"})
        assert "OPENAI_ORG_ID" not in result

    def test_strips_openai_api_base(self):
        result = self._clean({"OPENAI_API_BASE": "https://api.example.com", "PATH": "/usr/bin"})
        assert "OPENAI_API_BASE" not in result

    def test_strips_all_openai_prefix_vars(self):
        env = {
            "OPENAI_API_KEY": "sk-1",
            "OPENAI_ORGANIZATION": "org-1",
            "OPENAI_PROJECT": "proj-1",
            "OPENAI_BASE_URL": "https://api.openai.com",
            "HOME": "/home/user",
        }
        result = self._clean(env)
        for k in env:
            if k.startswith("OPENAI_"):
                assert k not in result, f"{k} must be stripped"
        assert "HOME" in result  # safe vars preserved


    def test_strips_codex_api_key(self):
        result = self._clean({"CODEX_API_KEY": "cdx-test", "PATH": "/usr/bin"})
        assert "CODEX_API_KEY" not in result, (
            "CODEX_API_KEY must NEVER reach the codex subprocess"
        )

    def test_strips_codex_workspace_key(self):
        result = self._clean({"CODEX_WORKSPACE_KEY": "ws-key", "PATH": "/usr/bin"})
        assert "CODEX_WORKSPACE_KEY" not in result

    def test_strips_codex_scope_key_pattern(self):
        env = {
            "CODEX_API_KEY": "key1",
            "CODEX_TEAM_KEY": "key2",
            "CODEX_ENTERPRISE_KEY": "key3",
            "HOME": "/home/user",
        }
        result = self._clean(env)
        for k in ("CODEX_API_KEY", "CODEX_TEAM_KEY", "CODEX_ENTERPRISE_KEY"):
            assert k not in result, f"{k} must be stripped"
        assert "HOME" in result

    def test_codex_non_key_vars_preserved(self):
        result = self._clean({"CODEX_VERSION": "1.2.3", "PATH": "/usr/bin"})
        assert "CODEX_VERSION" in result


    def test_preserves_path(self):
        result = self._clean({"PATH": "/usr/bin:/usr/local/bin", "OPENAI_API_KEY": "sk-x"})
        assert "PATH" in result
        assert result["PATH"] == "/usr/bin:/usr/local/bin"

    def test_preserves_home(self):
        result = self._clean({"HOME": "/home/alice", "CODEX_API_KEY": "x"})
        assert "HOME" in result

    def test_empty_env(self):
        result = self._clean({})
        assert result == {}

    def test_no_billing_vars_passthrough(self):
        env = {"PATH": "/usr/bin", "HOME": "/home/user", "LANG": "en_US.UTF-8"}
        result = self._clean(env)
        assert result == env


    def test_no_api_key_var_reaches_subprocess(self):
        env = {
            "OPENAI_API_KEY": "sk-should-be-stripped",
            "OPENAI_ORG_ID": "org-should-be-stripped",
            "CODEX_API_KEY": "cdx-should-be-stripped",
            "CODEX_WORKSPACE_KEY": "ws-should-be-stripped",
            "HOME": "/home/user",
            "PATH": "/usr/local/bin:/usr/bin",
            "LANG": "en_US.UTF-8",
        }
        result = self._clean(env)

        billing_keys_found = [
            k for k in result
            if k.startswith("OPENAI_")
            or (k.startswith("CODEX_") and k.endswith("_KEY"))
            or k == "CODEX_API_KEY"
        ]
        assert billing_keys_found == [], (
            f"Billing key(s) reached the subprocess env: {billing_keys_found}. "
            "This is the hard merge gate — no API key may ever reach codex."
        )


class TestCleanCodexAppServerEnv:

    @staticmethod
    def _subscription_auth(home: Path) -> Path:
        home.mkdir(parents=True)
        auth = home / "auth.json"
        auth.write_text(json.dumps({
            "auth_mode": "chatgpt",
            "OPENAI_API_KEY": None,
            "tokens": {
                "access_token": "access",
                "refresh_token": "refresh",
                "id_token": "id",
                "account_id": "account",
            },
        }))
        auth.chmod(0o600)
        return auth

    def test_app_server_projects_only_subscription_auth_into_isolated_home(
        self, tmp_path,
    ):
        from subagents.codex_env import clean_codex_app_server_env

        source_home = tmp_path / "user-codex"
        source_auth = self._subscription_auth(source_home)
        (source_home / "config.toml").write_text("[mcp_servers.hostile]\n")
        (source_home / "plugins").mkdir()
        isolated_home = tmp_path / "corsia-codex"

        with patch.dict(os.environ, {
            "HOME": str(tmp_path / "user-home"),
            "PATH": "/usr/bin",
            "OPENAI_API_KEY": "must-not-pass",
        }, clear=True):
            result = clean_codex_app_server_env(
                source_home=source_home,
                isolated_home=isolated_home,
            )

        assert result["HOME"] == str(tmp_path / "user-home")
        assert result["CODEX_HOME"] == str(isolated_home.resolve())
        assert result["CODEX_SQLITE_HOME"] == str(isolated_home.resolve())
        assert "OPENAI_API_KEY" not in result
        projected_auth = isolated_home / "auth.json"
        assert projected_auth.is_symlink()
        assert projected_auth.resolve() == source_auth.resolve()
        assert (isolated_home / "config.toml").read_text() == ""
        assert not (isolated_home / "plugins").exists()
        assert isolated_home.stat().st_mode & 0o777 == 0o700

    @pytest.mark.parametrize(
        "payload",
        [
            None,
            {"auth_mode": "apikey", "OPENAI_API_KEY": "sk-test"},
            {"auth_mode": "chatgpt", "tokens": {}},
        ],
    )
    def test_app_server_fails_loudly_without_subscription_auth(
        self, tmp_path, payload,
    ):
        from subagents.codex_env import clean_codex_app_server_env

        source_home = tmp_path / "user-codex"
        source_home.mkdir()
        if payload is not None:
            (source_home / "auth.json").write_text(json.dumps(payload))

        with pytest.raises(
            RuntimeError, match="ChatGPT subscription auth",
        ) as exc_info:
            clean_codex_app_server_env(
                source_home=source_home,
                isolated_home=tmp_path / "corsia-codex",
            )
        assert "codex login" in str(exc_info.value)

    def test_app_server_does_not_replace_legacy_exec_environment_contract(
        self, tmp_path,
    ):
        source_home = tmp_path / "custom-codex"
        with patch.dict(os.environ, {
            "CODEX_HOME": str(source_home),
            "HOME": str(tmp_path),
            "PATH": "/usr/bin",
        }, clear=True):
            from subagents.codex_env import clean_codex_env

            result = clean_codex_env()

        assert result["CODEX_HOME"] == str(source_home)

    def test_app_server_replaces_hostile_preexisting_isolated_config(
        self, tmp_path,
    ):
        from subagents.codex_env import clean_codex_app_server_env

        source_home = tmp_path / "user-codex"
        self._subscription_auth(source_home)
        isolated_home = tmp_path / "corsia-codex"
        isolated_home.mkdir()
        (isolated_home / "config.toml").write_text(
            "[mcp_servers.hostile]\ncommand = 'hostile'\n"
        )

        clean_codex_app_server_env(
            source_home=source_home,
            isolated_home=isolated_home,
        )

        controlled = isolated_home / "config.toml"
        assert controlled.read_text() == ""
        assert not controlled.is_symlink()
        assert controlled.stat().st_mode & 0o777 == 0o600

    def test_app_server_removes_foreign_home_instruction_files(self, tmp_path):
        from subagents.codex_env import clean_codex_app_server_env

        source_home = tmp_path / "user-codex"
        self._subscription_auth(source_home)
        isolated_home = tmp_path / "corsia-codex"
        isolated_home.mkdir()
        (isolated_home / "AGENTS.md").write_text("foreign instructions\n")
        (isolated_home / "AGENTS.override.md").write_text("foreign override\n")

        clean_codex_app_server_env(
            source_home=source_home,
            isolated_home=isolated_home,
        )

        assert not (isolated_home / "AGENTS.md").exists()
        assert not (isolated_home / "AGENTS.override.md").exists()

    def test_app_server_catalog_strips_only_provider_native_multi_agent_mode(
        self, tmp_path,
    ):
        from subagents.codex_env import (
            clean_codex_app_server_env,
            governed_codex_model_catalog_path,
        )

        source_home = tmp_path / "user-codex"
        self._subscription_auth(source_home)
        source_catalog = {
            "client_version": "0.144.6",
            "etag": "catalog-etag",
            "models": [
                {
                    "slug": "gpt-sol",
                    "display_name": "Sol",
                    "context_window": 1000000,
                    "multi_agent_version": "v2",
                },
                {
                    "slug": "gpt-standard",
                    "display_name": "Standard",
                    "context_window": 258400,
                    "multi_agent_version": None,
                },
            ],
        }
        (source_home / "models_cache.json").write_text(
            json.dumps(source_catalog), encoding="utf-8",
        )
        isolated_home = tmp_path / "corsia-codex"

        clean_codex_app_server_env(
            source_home=source_home,
            isolated_home=isolated_home,
            govern_model_catalog=True,
        )

        governed_path = governed_codex_model_catalog_path(isolated_home)
        governed = json.loads(governed_path.read_text(encoding="utf-8"))
        assert governed["client_version"] == "0.144.6"
        assert governed["etag"] == "catalog-etag"
        assert [model["multi_agent_version"] for model in governed["models"]] == [
            None,
            None,
        ]
        assert governed["models"][0]["context_window"] == 1000000
        assert json.loads(
            (source_home / "models_cache.json").read_text(encoding="utf-8")
        ) == source_catalog

    def test_app_server_catalog_clears_forced_code_mode_tool_mode(
        self, tmp_path,
    ):
        """Forced Code Mode must not survive into the governed catalog.

        Under ``tool_mode='code_mode_only'`` the provider emits only
        ``exec``/``wait`` at the response level and every managed tool becomes
        an inner sandbox RPC. Those callIds never appear as a top-level
        ``function_call``/``custom_tool_call``, so ``RolloutReceiptCursor``
        can never pair a receipt with them and the tool request waits forever
        under the session lock. ``features.code_mode=false`` alone does not
        prevent this, because model metadata overrides it.
        """
        from subagents.codex_env import (
            clean_codex_app_server_env,
            governed_codex_model_catalog_path,
        )

        source_home = tmp_path / "user-codex"
        self._subscription_auth(source_home)
        source_catalog = {
            "client_version": "0.154.0",
            "models": [
                {"slug": "gpt-forced", "tool_mode": "code_mode_only"},
                {"slug": "gpt-plain", "tool_mode": None},
                {"slug": "gpt-other", "tool_mode": "some_future_mode"},
                {"slug": "gpt-absent"},
            ],
        }
        (source_home / "models_cache.json").write_text(
            json.dumps(source_catalog), encoding="utf-8",
        )
        isolated_home = tmp_path / "corsia-codex"

        clean_codex_app_server_env(
            source_home=source_home,
            isolated_home=isolated_home,
            govern_model_catalog=True,
        )

        governed = json.loads(
            governed_codex_model_catalog_path(isolated_home).read_text(
                encoding="utf-8",
            )
        )
        by_slug = {model["slug"]: model for model in governed["models"]}
        # The forcing value is cleared.
        assert by_slug["gpt-forced"]["tool_mode"] is None
        # Every other value is left exactly as discovered.
        assert by_slug["gpt-plain"]["tool_mode"] is None
        assert by_slug["gpt-other"]["tool_mode"] == "some_future_mode"
        assert "tool_mode" not in by_slug["gpt-absent"]
        # Governing is invocation-local; user truth is never rewritten.
        assert json.loads(
            (source_home / "models_cache.json").read_text(encoding="utf-8")
        ) == source_catalog

    def test_app_server_fails_closed_without_a_catalog_to_govern(
        self, tmp_path,
    ):
        from subagents.codex_env import clean_codex_app_server_env

        source_home = tmp_path / "user-codex"
        self._subscription_auth(source_home)

        with pytest.raises(RuntimeError, match="model catalog"):
            clean_codex_app_server_env(
                source_home=source_home,
                isolated_home=tmp_path / "corsia-codex",
                govern_model_catalog=True,
            )

    @pytest.mark.parametrize("cache_owner", ["default", "destination"])
    def test_app_server_uses_discovered_isolated_catalog_for_custom_home(
        self, tmp_path, monkeypatch, cache_owner,
    ):
        from subagents import codex_env

        source_home = tmp_path / "user-codex"
        self._subscription_auth(source_home)
        default_home = tmp_path / "default-runtime-codex"
        destination = tmp_path / "detached-codex"
        monkeypatch.setattr(
            codex_env,
            "_default_app_server_home",
            lambda: default_home,
        )
        catalog_home = default_home if cache_owner == "default" else destination
        catalog_home.mkdir(parents=True)
        (catalog_home / "models_cache.json").write_text(json.dumps({
            "client_version": "0.144.6",
            "models": [{
                "slug": "gpt-sol",
                "multi_agent_version": "v2",
            }],
        }))

        if cache_owner == "default":
            with pytest.raises(RuntimeError, match="model catalog"):
                codex_env.clean_codex_app_server_env(
                    source_home=source_home,
                    isolated_home=destination,
                    govern_model_catalog=True,
                )
            return

        codex_env.clean_codex_app_server_env(
            source_home=source_home,
            isolated_home=destination,
            govern_model_catalog=True,
        )

        governed = json.loads(
            codex_env.governed_codex_model_catalog_path(destination).read_text()
        )
        assert governed["models"][0]["multi_agent_version"] is None

    def test_app_server_does_not_fall_back_from_invalid_newest_catalog(
        self, tmp_path, monkeypatch,
    ):
        from subagents import codex_env

        source_home = tmp_path / "user-codex"
        self._subscription_auth(source_home)
        destination = tmp_path / "detached-codex"
        destination.mkdir()
        valid = destination / "models_cache.json"
        valid.write_text(json.dumps({
            "models": [{"slug": "older", "multi_agent_version": "v2"}],
        }))
        invalid = source_home / "models_cache.json"
        invalid.write_text("{not-json")
        os.utime(valid, ns=(1, 1))
        os.utime(invalid, ns=(2, 2))

        with pytest.raises(RuntimeError, match=str(invalid)):
            codex_env.clean_codex_app_server_env(
                source_home=source_home,
                isolated_home=tmp_path / "detached-codex",
                govern_model_catalog=True,
            )



class TestCodexAppServerSessionEnvGate:

    def test_custom_home_uses_explicit_subscription_auth_source(self, tmp_path):
        from subagents.codex_app_server import CodexAppServerSession

        source = tmp_path / "source-codex"
        TestCleanCodexAppServerEnv._subscription_auth(source)
        (source / "models_cache.json").write_text(json.dumps({
            "models": [{"slug": "gpt-luna", "multi_agent_version": None}],
        }))
        destination = tmp_path / "checkpoint-codex"
        session = CodexAppServerSession(
            binary="codex",
            task=None,
            codex_home=str(destination),
            source_codex_home=str(source),
        )

        env = session.build_env()

        assert env["CODEX_HOME"] == str(destination)
        assert (destination / "auth.json").resolve() == (source / "auth.json").resolve()
        assert (destination / "corsia-model-catalog.json").is_file()

    def test_start_uses_isolated_app_server_environment(self):
        import asyncio

        from subagents.codex_app_server import CodexAppServerSession

        isolated_env = {
            "HOME": "/home/user",
            "CODEX_HOME": "/corsia/codex",
            "CODEX_SQLITE_HOME": "/corsia/codex",
        }
        captured_env: dict | None = None

        async def fail_spawn(*_args, **kwargs):
            nonlocal captured_env
            captured_env = kwargs.get("env")
            raise FileNotFoundError("stop after env capture")

        session = CodexAppServerSession(binary="codex", task=None, codex_home="/corsia/codex", source_codex_home="/account/codex")
        # Admission is covered separately; isolate the spawn environment here.
        with patch("subagents.vendor_write_admission.prepare_session_launch"), patch(
            "subagents.codex_env.clean_codex_app_server_env",
            return_value=isolated_env,
        ) as build_env:
            with patch(
                "asyncio.create_subprocess_exec", side_effect=fail_spawn,
            ):
                with pytest.raises(FileNotFoundError, match="env capture"):
                    asyncio.run(session.start())

        build_env.assert_called_once_with(source_home=Path("/account/codex"), isolated_home=Path("/corsia/codex"), govern_model_catalog=True, confined=False)
        assert captured_env == isolated_env
