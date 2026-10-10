"""Which ``corsia`` commands mint the lifecycle service token.

Minting writes a token row, so only commands that call the drain API may mint.
The predicate is read from the real scripts rather than restated here.
"""

from __future__ import annotations

import re
import subprocess
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[2]
CORSIA = REPO / "scripts" / "corsia"
ACCOUNTS = REPO / "scripts" / "corsia.d" / "accounts.sh"


def _lifecycle_predicate() -> str:
    """Return the source of ``is_mutating_lifecycle_command`` from scripts/corsia."""
    match = re.search(
        r"^is_mutating_lifecycle_command\(\) \{\n.*?^\}\n",
        CORSIA.read_text(),
        re.MULTILINE | re.DOTALL,
    )
    assert match, "is_mutating_lifecycle_command not found in scripts/corsia"
    return match.group(0)


def _needs_token(*args: str) -> bool:
    """Run ``needs_service_token`` with ``args`` in a clean bash."""
    script = f'{_lifecycle_predicate()}\n. "{ACCOUNTS}"\nneeds_service_token "$@"'
    result = subprocess.run(
        ["bash", "-c", script, "bash", *args], capture_output=True, check=False
    )
    return result.returncode == 0


@pytest.mark.parametrize(
    "args",
    [("stop",), ("restart",), ("redeploy",), ("restart-prepared",),
     ("service", "stop"), ("service", "restart")],
)
def test_draining_commands_mint(args):
    assert _needs_token(*args)


@pytest.mark.parametrize(
    "args",
    [(), ("status",), ("help",), ("logs",), ("user", "add"), ("token", "create"),
     ("service", "status"), ("ensure-current-release",), ("delegate", "list")],
)
def test_other_commands_do_not_mint(args):
    assert not _needs_token(*args)


def _selected_token(caller: str) -> str:
    """Run the token-selection block for ``restart`` with ``caller`` as the request caller."""
    match = re.search(
        r'^if \[ -n "\$_CORSIA_REQUEST_CALLER_TOKEN" \]; then\n.*?^fi\n',
        CORSIA.read_text(),
        re.MULTILINE | re.DOTALL,
    )
    assert match, "caller-token selection block not found in scripts/corsia"
    script = (
        f"_CORSIA_REQUEST_CALLER_TOKEN={caller!r}\n"
        "needs_service_token() { return 0; }\n"
        "mint_service_token() { echo minted; }\n"
        f'{match.group(0)}printf %s "$CORSIA_TOKEN"'
    )
    result = subprocess.run(
        ["bash", "-c", script, "bash", "restart"],
        capture_output=True, check=True, text=True, env={"PATH": "/usr/bin:/bin"},
    )
    return result.stdout


def test_updated_script_reuses_the_requesting_backends_caller():
    """Settings > Update from an older backend drains it with that backend's own credential."""
    assert _selected_token("caller-credential") == "caller-credential"


def test_cli_restart_mints_when_no_caller():
    assert _selected_token("") == "minted"
