"""Every lifecycle entry can find installed tools from a fresh shell."""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from tests.service_script_fixtures import fake_checkout


@pytest.mark.parametrize("platform", ["Darwin", "Linux"])
@pytest.mark.parametrize("custom_node", [False, True])
@pytest.mark.parametrize(
    ("command", "install_flow"),
    [("start", "1"), ("start", "0"), ("restart", "0"), ("ensure-current-release", "0")],
)
def test_lifecycle_children_resolve_tools_missing_from_ambient_path(
    tmp_path, platform, custom_node, command, install_flow
):
    home = tmp_path / "home"
    checkout = fake_checkout(home / ".corsia")
    (checkout / "config.json").write_text("{}")
    ambient = tmp_path / "ambient"
    ambient.mkdir()
    for name in ("dirname", "readlink"):
        (ambient / name).symlink_to(Path("/usr/bin") / name)
    uname = ambient / "uname"
    uname.write_text(f"#!/bin/sh\nprintf '%s\\n' {platform}\n")
    uname.chmod(0o755)
    if custom_node:
        node = ambient / "node"
        node.write_text("#!/bin/sh\nprintf 'selected-node\\n'\n")
        node.chmod(0o755)

    tools = tmp_path / "service-tools"
    tools.mkdir()
    for name, body in {
        "node": "printf 'resolved-node\\n'",
        "npm": "#!/usr/bin/env node",
        "brew": "printf 'resolved-brew\\n'",
    }.items():
        path = tools / name
        path.write_text(body + "\n" if name == "npm" else "#!/bin/sh\n" + body + "\n")
        path.chmod(0o755)
    for name in ("service.sh", "systemd.sh"):
        path = checkout / "scripts" / "corsia.d" / name
        lines = path.read_text().splitlines()
        lines = [
            f'SERVICE_PATH="{tools}"' if line.startswith("SERVICE_PATH=") else line
            for line in lines
        ]
        path.write_text("\n".join(lines) + "\n")

    # Observe child tool resolution after the real platform initialization,
    # before any credential, service, process or database effects.
    accounts = checkout / "scripts" / "corsia.d" / "accounts.sh"
    accounts.write_text(accounts.read_text() + "\nneeds_service_token() { return 1; }\n")
    delegate = checkout / "scripts" / "corsia.d" / "delegate.sh"
    delegate.write_text(delegate.read_text() + "\nnpm --version\nbrew --version\nexit 0\n")

    result = subprocess.run(
        ["/bin/bash", str(checkout / "scripts" / "corsia"), command],
        env={"HOME": str(home), "PATH": str(ambient), "CORSIA_INSTALL_FLOW": install_flow},
        text=True,
        capture_output=True,
        timeout=30,
    )

    assert result.returncode == 0, result.stderr
    expected_node = "selected-node" if custom_node else "resolved-node"
    assert result.stdout.splitlines() == [expected_node, "resolved-brew"]
