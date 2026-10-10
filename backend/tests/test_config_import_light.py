"""The import contract ``scripts/corsia service install`` depends on.

The installer resolves workspace and state identity by importing
``infra.config`` with the interpreter of a machine where ``requirements.txt``
has never been installed. Anything that module reaches at import time must
therefore be the standard library or first-party code. This is easy to break by
accident -- a single convenience import of a module that happens to need a
third-party package puts that package on the critical path of a fresh install --
and it breaks only on a machine nobody develops on, so it is pinned here.
"""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

BACKEND = Path(__file__).resolve().parent.parent

# Imports ``infra.config`` with psutil made genuinely unavailable, the way a
# machine that has never installed the dependencies sees it.
IMPORT_WITHOUT_PSUTIL = """
import sys


class BlockPsutil:
    def find_spec(self, name, path=None, target=None):
        if name == "psutil" or name.startswith("psutil."):
            raise ImportError("No module named 'psutil'")
        return None


sys.meta_path.insert(0, BlockPsutil())
import infra.config  # noqa: F401

pulled = sorted(m for m in sys.modules if m == "psutil" or m.startswith("psutil."))
assert not pulled, f"importing infra.config pulled in {pulled}"
print("imported")
"""


def _import_config(source: str) -> subprocess.CompletedProcess:
    """Import infra.config in a clean interpreter and report what happened."""
    env = {
        **os.environ,
        "PYTHONPATH": str(BACKEND),
        "CORSIA_TEST_MODE": "1",
    }
    return subprocess.run(
        [sys.executable, "-c", source],
        capture_output=True,
        text=True,
        env=env,
        cwd=str(BACKEND),
    )


def test_config_imports_on_a_machine_that_has_no_dependencies_yet():
    """A fresh install must get through this import before it can install."""
    result = _import_config(IMPORT_WITHOUT_PSUTIL)

    assert result.returncode == 0, result.stderr
    assert "imported" in result.stdout


def test_importing_config_does_not_pull_in_a_third_party_package():
    """Passing only because psutil is installed would prove nothing.

    The previous test blocks psutil, so it also passes if some other module
    imports it lazily and swallows the failure. This one runs with psutil
    importable and asserts it stayed out of ``sys.modules`` anyway, which is the
    property that keeps the installer working: not that the failure is handled,
    but that the package is never reached.
    """
    result = _import_config(
        "import sys\n"
        "import infra.config  # noqa: F401\n"
        "pulled = sorted(m for m in sys.modules if m.split('.')[0] == 'psutil')\n"
        "assert not pulled, f'importing infra.config pulled in {pulled}'\n"
        "print('clean')\n"
    )

    assert result.returncode == 0, result.stderr
    assert "clean" in result.stdout
