"""Carry workspace conductor role additions to the CORSIA agent, once.

The workspace-scope agent used to receive the ``conductor`` role, so its
workspace role additions live in ``<workspace>/context/roles/conductor/``.
It now receives ``corsia_agent``, and that folder reaches only project
conductors. At boot, a workspace with conductor additions and no
``corsia_agent`` folder gets a copy, so neither audience loses what was
written for it. The copy runs only while the target is absent, so a later
edit or deletion there is never overwritten.
"""

from __future__ import annotations

import logging
import shutil
from pathlib import Path

from core.role_context_sources import role_directory

log = logging.getLogger(__name__)


def carry_workspace_conductor_role_files(workspace: Path) -> bool:
    """Copy ``context/roles/conductor`` to ``context/roles/corsia_agent``.

    Returns True when it copied. A missing or empty source, an existing
    target, or a symlinked container leaves the workspace untouched.
    """
    try:
        source = role_directory(workspace, "conductor")
        target = role_directory(workspace, "corsia_agent")
    except RuntimeError as error:
        log.warning("corsia_agent role files not carried: %s", error)
        return False
    if target.exists() or not source.is_dir() or not any(source.iterdir()):
        return False
    shutil.copytree(source, target, symlinks=True)
    log.info("corsia_agent role files carried from %s", source)
    return True
