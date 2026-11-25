"""Content preconditions for durable shared-file publication."""

from pathlib import Path

from doceditor.document_save_errors import DocumentSaveError, FileChangedOutside, MISSING_DOCUMENT_MESSAGE
from registry.identity_metadata import sha256_file
from tools.filesystem_effects import restore_claimed_file


def check_expected_content(path: Path, expected_sha256: str | None) -> None:
    """Refuse publication when an expected target has changed or disappeared."""
    if expected_sha256 is None:
        return
    try:
        actual = sha256_file(path)
    except FileNotFoundError as exc:
        raise DocumentSaveError("no_such_file", MISSING_DOCUMENT_MESSAGE) from exc
    except IsADirectoryError:
        raise FileChangedOutside() from None
    if actual != expected_sha256:
        raise FileChangedOutside()


def replace_if_expected(stage: Path, target: Path, expected_sha256: str | None) -> None:
    """Check the target immediately before replacing it with the staged file.

    Any writer can still change the target between the check and rename.
    """
    from tools.filesystem_effects import replace_bound_file

    check_expected_content(target, expected_sha256)
    replace_bound_file(stage, target)


def restore_if_owned(stage: Path, target: Path, content_hash: str) -> None:
    """Restore only while the target still holds this publication's bytes."""
    try:
        replace_if_expected(stage, target, content_hash)
    except DocumentSaveError as exc:
        if exc.kind not in ("stale", "no_such_file"):
            raise
        # The outside edit or deletion owns the target; finish undo around it.


def checked_file_mode(path: Path, expected_sha256: str | None) -> int:
    """Get target mode, preserving the missing-target refusal for conditional writes."""
    from tools.filesystem_effects import bound_file_mode

    try:
        return bound_file_mode(path)
    except FileNotFoundError:
        check_expected_content(path, expected_sha256)
        raise


def checked_copy_target(path: Path, backup: Path, expected_sha256: str | None) -> None:
    """Copy a target, preserving its missing-target refusal during a conditional write."""
    from tools.filesystem_effects import copy_bound_file

    try:
        copy_bound_file(path, backup)
    except FileNotFoundError:
        check_expected_content(path, expected_sha256)
        raise


def _publish_stage(target: Path, stage: Path, backup: Path | None, expected_sha256: str | None) -> None:
    check_expected_content(target, expected_sha256)
    if backup is not None:
        checked_copy_target(target, backup, expected_sha256)
    if backup is not None:
        replace_if_expected(stage, target, expected_sha256)
    elif not restore_claimed_file(stage, target):
        raise FileExistsError(f"Publication destination already exists: {target}")
