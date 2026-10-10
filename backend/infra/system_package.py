"""Location of the packaged, read-only CORSIA System tier.

A leaf module and the one owner of where the package lives: the directory on
disk and the package reference every context row cites. The System loader and
the workspace projection both take the source location from here.

The published coordination models stay canonical in ``docs/coordination-models``;
the manifest declares them under ``coordination-models/`` so they are projected
and drift-checked as readable options, never loaded as System context.
"""

from pathlib import Path, PurePosixPath

SYSTEM_VERSION = "v1"
SYSTEM_PACKAGE_NAME = "prompts.system"
# backend/infra/system_package.py -> backend/prompts/system/<version>/
_PACKAGE_DIR = (
    Path(__file__).resolve().parents[1] / "prompts" / "system" / SYSTEM_VERSION
)
COORDINATION_MODELS_ROOT = "coordination-models"
# backend/infra/system_package.py -> docs/coordination-models/
_COORDINATION_MODELS_DIR = (
    Path(__file__).resolve().parents[2] / "docs" / COORDINATION_MODELS_ROOT
)


def system_package_dir() -> Path:
    """The one locator: every read of the System package resolves through here."""
    return _PACKAGE_DIR


def coordination_models_dir() -> Path:
    """The canonical published coordination models declared by the manifest."""
    return _COORDINATION_MODELS_DIR


def system_resource_source(relative_path: str | PurePosixPath) -> Path:
    """The source file for one manifest path; callers validate the path first."""
    path = PurePosixPath(relative_path)
    if path.parts and path.parts[0] == COORDINATION_MODELS_ROOT:
        return coordination_models_dir().joinpath(*path.parts[1:])
    return system_package_dir().joinpath(*path.parts)


def system_package_ref(relative_path: str = "") -> str:
    """The ``package://`` reference for the System package or a file inside it."""
    base = f"package://{SYSTEM_PACKAGE_NAME}/{SYSTEM_VERSION}"
    return f"{base}/{relative_path}" if relative_path else base
