"""The unfinished-setup marker: the app shell's note that setup began here and was never finished.

The desktop shell writes ``setup-unfinished`` in the app folder (the state root's parent) only when its first
run makes a brand-new state root (``desktop/src-tauri/src/unfinished_setup.rs``). While the file exists, the
next launch moves that state root aside and deletes it, keeping the downloads in it (the model store, classifier
assets and logs), and removes everything the marker lists, so an unfinished setup starts again at step 1 as a
fresh install.

The backend has two jobs, both on that one file:

* when the app's setup creates a file in the work folder, it records the path and digest there, so the shell
  removes it only while its bytes still match;
* it removes the file whenever the install comes to read as finished (``setup_progress.view()``, the one
  answer to that), and *before* the change is committed: when the person finishes setup, when the first
  account is made outside the app (``corsia user add``), and at backend startup. So a finished install can
  never be wiped. If the file cannot be removed, finishing fails and setup stays unfinished.

Only a marker naming this backend's own state root is touched. Any other install, or one the shell never marked,
has no marker and every function here does nothing.

One file, two writers, each with its own fields:

| Field | Written by | Meaning |
|---|---|---|
| ``state_dir``, ``workspace`` | the shell, when its first run makes the state root | the one state folder it may wipe, and the folder that first run made |
| ``files_root``, ``files`` | this module | the folder setup's files are relative to (the chosen work folder), and each file with its digest |

The backend deletes the marker when setup finishes. The shell replaces it when it marks the new first run it plans
after a wipe. A marker this module cannot read or parse is handled the way the shell handles it: it records
nothing in it (the shell never wipes on it, and discards a damaged one at its next launch), but finishing deletes
it, so a finished install never keeps a marker it could not verify.
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


def _read() -> tuple[str, dict | None]:
    """What the marker file is to this install: ``none``, ``unusable`` (unreadable or damaged), ``other`` or ``ours``."""
    try:
        data = json.loads(marker_path().read_bytes())
    except FileNotFoundError:
        return "none", None
    except (OSError, ValueError):
        logger.warning("The unfinished-setup marker cannot be read", exc_info=True)
        return "unusable", None
    state_dir = data.get("state_dir") if isinstance(data, dict) else None
    if not isinstance(state_dir, str) or Path(state_dir).resolve() != config.STATE_ROOT.resolve():
        return "other", None
    return "ours", data


def record_created_file(relative: str, sha256: str) -> None:
    """Add a work-folder file this setup created, with the digest the shell checks before removing it.

    Does nothing when this install has no usable marker of its own. Raises ``OSError`` when the marker cannot be
    rewritten, so the caller can refuse to create the file.
    """
    _record(relative, sha256, only_if_recorded=False)


def update_created_file(relative: str, sha256: str) -> None:
    """Replace the digest of a file this setup created and the person then edited on setup's Back step.

    Does nothing for a file the marker does not list, so a file the person had before setup is never added.
    Raises ``OSError`` when the marker cannot be rewritten.
    """
    _record(relative, sha256, only_if_recorded=True)


def _record(relative: str, sha256: str, *, only_if_recorded: bool) -> None:
    _, data = _read()
    if data is None or data.get("wiped") is True:
        return
    recorded = data.get("files")
    files = recorded if isinstance(recorded, dict) else {}
    if only_if_recorded and relative not in files:
        return
    root = str(config.WORKSPACE)
    if files and data.get("files_root") != root:
        raise OSError("setup files are recorded for another work folder, so this one cannot be added")
    data = {**data, "files_root": root, "files": {**files, relative: sha256}}
    path = marker_path()
    staged = path.with_name(f"{MARKER_FILE}.tmp")
    staged.write_text(json.dumps(data, indent=2), encoding="utf-8")
    os.replace(staged, path)


def remove() -> None:
    """Delete this install's marker, so the shell never wipes it. Raises ``OSError`` if it cannot be deleted.

    A marker that cannot be read or parsed is deleted too: it cannot be shown to be another install's, and a finished
    install must not keep one. Another install's marker is left alone.
    """
    kind, _ = _read()
    if kind in ("ours", "unusable"):
        marker_path().unlink(missing_ok=True)
