"""The unfinished-setup marker: the app shell's note that setup began here and was never finished.

The desktop shell writes ``setup-unfinished`` in the app folder (the state root's parent) only when its first
run makes a brand-new state root (``desktop/src-tauri/src/unfinished_setup.rs``). While the file exists, the
next launch removes that state root and everything the marker lists, so an unfinished setup starts again at
step 1 as a fresh install.

The backend has two jobs, both on that one file:

* when the app's setup creates a file in the work folder, it records the path and digest there, so the shell
  removes it only while its bytes still match;
* when the person finishes setup it removes the file *before* the completion is committed, so a finished
  install can never be wiped. If the file cannot be removed, finishing fails and setup stays unfinished.

Only a marker naming this backend's own state root is touched. Any other install, or one the shell never marked,
has no marker and every function here does nothing.
"""

from __future__ import annotations

import json
import logging
import os
from pathlib import Path

from infra import config

logger = logging.getLogger(__name__)

#: The marker's name in the app folder. Held equal to ``MARKER_FILE`` in the shell by a parity test.
MARKER_FILE = "setup-unfinished"


def marker_path() -> Path:
    return config.STATE_ROOT.parent / MARKER_FILE


def _own_marker() -> dict | None:
    """The marker this install's shell wrote for this state root, else ``None``."""
    try:
        data = json.loads(marker_path().read_bytes())
    except FileNotFoundError:
        return None
    except (OSError, ValueError):
        logger.warning("The unfinished-setup marker could not be read; it is left as it is", exc_info=True)
        return None
    state_dir = data.get("state_dir") if isinstance(data, dict) else None
    if not isinstance(state_dir, str) or Path(state_dir).resolve() != config.STATE_ROOT.resolve():
        return None
    return data


def record_created_file(relative: str, sha256: str) -> None:
    """Add a work-folder file this setup created, with the digest the shell checks before removing it.

    Does nothing when this install has no marker. Raises ``OSError`` when the marker cannot be rewritten,
    so the caller can refuse to create the file.
    """
    data = _own_marker()
    if data is None:
        return
    files = data.get("files") if isinstance(data.get("files"), dict) else {}
    data = {**data, "workspace": str(config.WORKSPACE), "files": {**files, relative: sha256}}
    path = marker_path()
    staged = path.with_name(f"{MARKER_FILE}.tmp")
    staged.write_text(json.dumps(data, indent=2), encoding="utf-8")
    os.replace(staged, path)


def remove() -> None:
    """Delete this install's marker, so the shell never wipes it. Raises ``OSError`` if it cannot be deleted."""
    if _own_marker() is not None:
        marker_path().unlink()
