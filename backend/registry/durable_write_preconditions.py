"""Content preconditions for durable shared-file publication."""

from pathlib import Path

from doceditor.document_save_errors import DocumentSaveError, FileChangedOutside
from registry.identity_metadata import sha256_file


def check_expected_content(path: Path, expected_sha256: str | None) -> None:
    """Refuse publication when an expected target has changed or disappeared."""
    if expected_sha256 is None:
        return
    try:
        actual = sha256_file(path)
    except FileNotFoundError as exc:
        raise DocumentSaveError("no_such_file", f"No such file: {exc}") from exc
    except IsADirectoryError:
        raise FileChangedOutside() from None
    if actual != expected_sha256:
        raise FileChangedOutside()


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
