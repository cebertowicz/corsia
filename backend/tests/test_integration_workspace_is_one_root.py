"""The integration workspace is the one root every module in the process sees.

``integration_workspace`` is session-scoped. It used to reload ``infra.config``
onto a second directory, which left modules that imported ``WORKSPACE`` by value
on the first. A later test's workspace-relative read then resolved in one root
and was refused as outside the other (#2949).
"""

from __future__ import annotations

from pathlib import Path

# Imported at collection, before the fixture runs, as every module a test
# session has already loaded is.
from tools import sandbox_config


def test_the_integration_workspace_is_the_configured_and_authorized_root(
    integration_workspace,
):
    from infra import config

    assert integration_workspace == config.WORKSPACE
    assert config.WORKSPACE in sandbox_config._STATIC_ALLOWED_RW
    assert sandbox_config.is_path_allowed(Path(integration_workspace) / "notes.md")
