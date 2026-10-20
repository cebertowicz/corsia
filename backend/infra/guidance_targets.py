"""Guidance path classification and its stable target value."""

from __future__ import annotations

from contextlib import closing
from dataclasses import dataclass
from pathlib import Path

from loguru import logger


@dataclass(frozen=True)
class GuidanceTarget:
    """A guidance save's scope label, or the fault that hid it."""

    scope: str
    fault: str | None = None


def guidance_target(path: Path, workspace: Path | None = None) -> GuidanceTarget | None:
    """Return a guidance target, preserving classification faults for the caller."""
    from core.guidance_paths import classify_guidance_path
    from infra.config import WORKSPACE
    from infra.database import get_db

    try:
        with closing(get_db()) as db:
            scope = classify_guidance_path(path, workspace=workspace or Path(WORKSPACE), db=db)
    except Exception as exc:
        logger.warning("guidance classification failed for {}: {}", path, exc)
        return GuidanceTarget("unknown", f"could not check whether this file is guidance ({exc})")
    return GuidanceTarget(scope) if scope else None
