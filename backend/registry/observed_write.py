"""Confirmed file-write registration from settled observer results."""

from __future__ import annotations

from pathlib import Path

from infra import config
from registry.identity_metadata import sha256_file
from registry.service import register_write
from tools.filesystem_rw import write_success_path


# Claude Code's builtins report the same canonical writer names but carry their own
# argument-key schema (``file_path`` vs. our native ``path``); this is the one place
# that fact is recorded.
_CC_BUILTIN_PATH_KEY = "file_path"


def _written_hash(path: Path) -> dict | None:
    """The tool is this file's writer, so it states the hash it wrote.

    Registration itself never replaces a stored hash.
    """
    if not path.is_file() or path.is_symlink():
        return None
    try:
        return {"last_written_sha256": sha256_file(path)}
    except OSError:
        return None


def register_observed_write(
    thread_id, args, arg_keys, text, *, name, is_cc_builtin, origin, db,
) -> dict | None:
    """Register one confirmed write.

    Resolves the path the tool actually wrote — preferring the path confirmed
    by the tool's own success receipt over the requested argument — and
    returns the registered artifact, or ``None`` when no path was written.
    """
    path_key, desc_key = arg_keys
    if is_cc_builtin:
        path_key = _CC_BUILTIN_PATH_KEY
    raw_path = args.get(path_key)
    if not raw_path:
        return None
    confirmed_path = (
        write_success_path(text)
        if name == "write_file" and not is_cc_builtin
        else None
    )
    path = confirmed_path or Path(str(raw_path)).expanduser()
    if not path.is_absolute():
        path = config.WORKSPACE / path
    description = args.get(desc_key) if desc_key else None
    artifact = register_write(
        thread_id, str(path), description, origin=origin, metadata=_written_hash(path), db=db, notify=False
    )
    # Context-source notification is completed by the file tools immediately
    # after the save, before this late registration observer is invoked.
    return artifact
