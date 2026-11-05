"""Managed oMLX binary resolution for packaged CORSIA."""

from __future__ import annotations

from pathlib import Path

from llm import managed_omlx_binary


def test_packaged_resolver_uses_the_bundled_executable_not_path(tmp_path, monkeypatch):
    bundle = tmp_path / "CORSIA.app"
    executable = bundle / "Contents" / "Resources" / "bin" / "omlx"
    executable.parent.mkdir(parents=True)
    executable.write_text("#!/bin/sh\n", encoding="utf-8")
    monkeypatch.setattr(managed_omlx_binary, "app_bundle_root", lambda *_args: bundle)

    assert managed_omlx_binary.managed_omlx_executable() == str(executable)
    assert managed_omlx_binary.is_managed_omlx_executable(executable)
    assert not managed_omlx_binary.is_managed_omlx_executable("/opt/homebrew/bin/omlx")


def test_missing_bundled_omlx_is_a_recoverable_app_damage_state(tmp_path, monkeypatch):
    monkeypatch.setattr(managed_omlx_binary, "app_bundle_root", lambda *_args: tmp_path / "CORSIA.app")

    assert managed_omlx_binary.managed_omlx_executable() is None
    assert managed_omlx_binary.managed_omlx_install_error() == (
        "The CORSIA app is damaged because its bundled oMLX executable is missing. "
        "Download CORSIA again, replace this app, then try again."
    )


def test_changing_the_release_pin_keeps_the_existing_runtime(tmp_path, monkeypatch):
    from llm import omlx_release

    monkeypatch.setattr("infra.config.STATE_ROOT", tmp_path)
    before = managed_omlx_binary.source_omlx_environment()
    monkeypatch.setattr(omlx_release, "OMLX_VERSION", "99.0")
    assert managed_omlx_binary.source_omlx_environment() == before


def test_source_executable_is_visible_only_after_install_completes(tmp_path, monkeypatch):
    environment = tmp_path / "runtime"
    executable = environment / "bin" / "omlx"
    executable.parent.mkdir(parents=True)
    executable.write_text("#!/bin/sh\n", encoding="utf-8")
    monkeypatch.setattr(managed_omlx_binary, "app_bundle_root", lambda *_args: None)
    monkeypatch.setattr(managed_omlx_binary, "source_omlx_environment", lambda: environment)
    assert managed_omlx_binary.managed_omlx_executable() is None
    (environment / ".installed").touch()
    assert managed_omlx_binary.managed_omlx_executable() == str(executable)
    assert managed_omlx_binary.is_managed_omlx_executable(executable)


def test_source_install_has_no_bundled_managed_executable(monkeypatch):
    monkeypatch.setattr(managed_omlx_binary, "app_bundle_root", lambda *_args: None)

    assert managed_omlx_binary.managed_omlx_executable() is None
    assert not managed_omlx_binary.is_managed_omlx_executable("/opt/homebrew/bin/omlx")
