"""Content preconditions for durable shared-file publication."""

from pathlib import Path

from doceditor.document_save_errors import FileChangedOutside
from registry.identity_metadata import sha256_file


def check_expected_content(path: Path, expected_sha256: str | None) -> None:
    """Refuse publication when an expected target has changed or disappeared."""
    if expected_sha256 is not None and (
        not path.is_file() or sha256_file(path) != expected_sha256
    ):
        raise FileChangedOutside()
