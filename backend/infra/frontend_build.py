"""Verify and recover the exact frontend release before request serving.

Lifespan calls this adapter before database initialization. The checkout-local
CORSIA script owns the Update mutex, prepared-stage validation, build, and
activation so startup cannot create a second frontend lifecycle authority.
"""

from __future__ import annotations

import os
from pathlib import Path
import subprocess
import sys

from infra.config import FRONTEND_DIR, STATE_ROOT, WORKSPACE

FRONTEND_ROOT = FRONTEND_DIR.parent
_SERVING_ROOT = FRONTEND_ROOT.parent
_CORSIA_SCRIPT = _SERVING_ROOT / "scripts" / "corsia"


def ensure_frontend_built() -> None:
    """Require frontend assets paired to this checkout before lifespan proceeds."""
    if not FRONTEND_ROOT.is_dir():
        return

    env = os.environ.copy()
    env["CORSIA_DIR"] = str(_SERVING_ROOT)
    env["CORSIA_PYTHON"] = sys.executable
    env["CORSIA_WORKSPACE"] = str(WORKSPACE)
    env["CORSIA_STATE_DIR"] = str(STATE_ROOT)
    try:
        result = subprocess.run(
            [str(_CORSIA_SCRIPT), "ensure-current-release"],
            cwd=_SERVING_ROOT,
            env=env,
            check=False,
        )
    except OSError as exc:
        raise RuntimeError(
            f"frontend release pairing failed: could not run {_CORSIA_SCRIPT}: {exc}"
        ) from exc
    if result.returncode != 0:
        raise RuntimeError(
            "frontend release pairing failed: "
            f"release owner exited {result.returncode}"
        )
