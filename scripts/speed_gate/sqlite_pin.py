"""One SQLite build for every measurement, on every host.

VM step counts differ between SQLite releases, and Python links whatever SQLite the host ships (3.45 on the
hosted Linux image, a different one on a Mac). The gate therefore measures on a pinned build: the
``sqlean.py`` wheel (``scripts/speed_gate/requirements.txt``, exact version and hashes), installed for
this gate only, never a runtime requirement and never imported by product code. ``install`` makes it the
``sqlite3`` module for this process before anything imports the application, and refuses any other
SQLite version, so a measurement on the wrong build stops instead of producing counts that are not comparable.

Where ``sqlean`` is installed outside the interpreter's own packages (``scripts/check-pr`` provisions it
into a worktree-local directory), ``SPEED_GATE_SQLEAN_PATH`` names that directory.
"""

from __future__ import annotations

import os
import sys
from types import ModuleType

#: The ``sqlean.py`` release in ``requirements.txt`` and the SQLite it bundles; change both together.
PINNED_PACKAGE = "sqlean.py==3.50.4.5"
PINNED_SQLITE = "3.50.4"
PATH_VARIABLE = "SPEED_GATE_SQLEAN_PATH"


class PinError(RuntimeError):
    """The pinned SQLite is missing or is not the pinned version; no measurement may run."""


def install() -> ModuleType:
    """Alias ``sqlite3`` to the pinned build for this process and return it."""
    extra = os.environ.get(PATH_VARIABLE)
    if extra:
        sys.path.append(extra)
    try:
        import sqlean
    except ImportError as error:
        raise PinError(
            f"{PINNED_PACKAGE} is not installed, so the SQLite build is not the pinned one. Install it with: "
            "pip install --require-hashes --only-binary :all: -r scripts/speed_gate/requirements.txt "
            f"(or name the directory holding it in {PATH_VARIABLE})") from error
    if sqlean.sqlite_version != PINNED_SQLITE:
        raise PinError(f"SQLite {sqlean.sqlite_version} is loaded but the gate pins {PINNED_SQLITE} "
                       f"({PINNED_PACKAGE}); counts from another build are not comparable")
    sys.modules["sqlite3"] = sqlean
    sys.modules["sqlite3.dbapi2"] = sqlean.dbapi2
    return sqlean


# The pinned module itself. Every speed-gate module that talks to SQLite imports this name instead of the
# standard library one, and importing it installs the pin, so no module can bind another build by mistake.
sqlite3: ModuleType = install()
