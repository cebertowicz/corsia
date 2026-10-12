"""Plan-node author-declared size validation."""

from __future__ import annotations

from core.size_rating import SIZE_CLASSES, SIZE_MEANING, SIZE_SCALE_DESCRIPTION
from plans import service


def validate_size(value: object) -> str | None:
    """Return a valid nullable declaration or an actionable input error."""
    if value is None:
        return None
    if not isinstance(value, str) or value not in SIZE_CLASSES:
        raise service.PlanInputError(
            f"size must be one of: {', '.join(SIZE_CLASSES)}. {SIZE_MEANING} {SIZE_SCALE_DESCRIPTION}."
        )
    return value
