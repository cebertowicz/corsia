"""A key variable set at launch is imported once, then gone from the process.

The SDKs fall back to ANTHROPIC_API_KEY / OPENAI_API_KEY when CORSIA passes no
key, so leaving them in the environment would keep access alive after the key
store is empty or a key is removed, while the UI says the variable is ignored.
"""

from __future__ import annotations

import pytest

from infra import ignored_environment, key_store_providers

VARIABLES = ("ANTHROPIC_API_KEY", "OPENAI_API_KEY", "BRAVE_API_KEY")
LAUNCH_VALUE = "launch-environment-value"


@pytest.fixture
def launch_environment(isolated_db, tmp_path, monkeypatch):
    root = tmp_path / "state"
    root.mkdir()
    monkeypatch.setattr("infra.config.STATE_ROOT", root)
    monkeypatch.setattr(ignored_environment, "_present", frozenset())
    for variable in VARIABLES:
        monkeypatch.setenv(variable, LAUNCH_VALUE)
    monkeypatch.setenv("UNRELATED_VARIABLE", "kept")


def test_dropping_removes_only_the_three_key_variables(launch_environment):
    import os

    ignored_environment.note_ignored_environment()
    ignored_environment.drop_ignored_environment()

    assert not any(variable in os.environ for variable in VARIABLES)
    assert os.environ["UNRELATED_VARIABLE"] == "kept"
    assert ignored_environment._present == {"provider-anthropic", "provider-openai", "web-search"}


def test_startup_drops_them_after_the_database_migration_has_read_them(launch_environment, monkeypatch, tmp_path):
    import os

    import app_lifecycle
    import core.project_path_conversion as conversion
    import core.system_projection as system_projection
    import infra.config as config
    import infra.guidance_git as guidance_git
    import infra.startup_prerequisites as prerequisites
    import registry.durable_writes as durable_writes
    import registry.relocate as relocate

    seen_by_migration: list[bool] = []
    for directory in ("UPLOADS_DIR", "LOGS_DIR", "RUNTIME_DIR"):
        monkeypatch.setattr(config, directory, tmp_path / directory)
    monkeypatch.setattr(prerequisites, "prepare_startup_prerequisites", lambda *_a, **_k: None)
    monkeypatch.setattr(
        app_lifecycle, "init_db", lambda: seen_by_migration.append("ANTHROPIC_API_KEY" in os.environ)
    )
    monkeypatch.setattr(conversion, "convert_legacy_project_paths", lambda *_a, **_k: None)
    monkeypatch.setattr(durable_writes, "recover_write_intents", lambda *_a, **_k: None)
    monkeypatch.setattr(relocate, "recover_interrupted_relocations", lambda *_a, **_k: None)
    monkeypatch.setattr(system_projection, "ensure_system_projection", lambda *_a, **_k: None)
    monkeypatch.setattr(guidance_git, "startup_sweep", lambda *_a, **_k: None)

    class QuietLogger:
        def info(self, *_args: object) -> None:
            pass

    app_lifecycle._initialize_startup(QuietLogger())

    assert seen_by_migration == [True]
    assert not any(variable in os.environ for variable in VARIABLES)



def test_with_the_store_empty_no_key_reaches_the_sdk_params(launch_environment):
    from llm.capabilities import make_provider_params

    ignored_environment.drop_ignored_environment()

    assert not make_provider_params("anthropic/claude-sonnet-4-5")["api_key"]
    assert not make_provider_params("openai/gpt-5")["api_key"]


def test_with_the_store_empty_the_openai_and_anthropic_clients_have_no_key(launch_environment):
    from openai import OpenAIError

    from llm.providers import anthropic as anthropic_provider
    from llm.providers import openai as openai_provider

    ignored_environment.drop_ignored_environment()
    openai_provider._CLIENTS.clear()
    anthropic_provider._CLIENTS.clear()

    with pytest.raises(OpenAIError):
        openai_provider._get_client(None)
    assert anthropic_provider._get_client(None, None).api_key is None


def test_removing_a_stored_key_stops_access(launch_environment):
    from llm.capabilities import make_provider_params

    key_store_providers.set_provider_key("anthropic", "stored-value")
    assert make_provider_params("anthropic/claude-sonnet-4-5")["api_key"] == "stored-value"

    ignored_environment.drop_ignored_environment()
    key_store_providers.remove_provider_key("anthropic")

    assert not make_provider_params("anthropic/claude-sonnet-4-5")["api_key"]
