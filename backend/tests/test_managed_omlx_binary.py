"""Managed oMLX binary resolution for packaged CORSIA."""

from __future__ import annotations

from pathlib import Path

import pytest

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


def test_missing_bundled_omlx_is_plain_app_damage(tmp_path, monkeypatch):
    monkeypatch.setattr(managed_omlx_binary, "app_bundle_root", lambda *_args: tmp_path / "CORSIA.app")

    with pytest.raises(RuntimeError, match="CORSIA app is damaged.*bundled oMLX executable"):
        managed_omlx_binary.managed_omlx_executable()


def test_source_install_has_no_bundled_managed_executable(monkeypatch):
    monkeypatch.setattr(managed_omlx_binary, "app_bundle_root", lambda *_args: None)

    assert managed_omlx_binary.managed_omlx_executable() is None
    assert not managed_omlx_binary.is_managed_omlx_executable("/opt/homebrew/bin/omlx")
