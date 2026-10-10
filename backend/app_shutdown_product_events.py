"""Product-event shutdown flushing."""

from __future__ import annotations


def flush_product_events(logger) -> None:
    """Flush product events without blocking later shutdown owners."""
    try:
        from infra.product_events import flush

        flush()
    except Exception as exc:
        logger.warning(f"shutdown: product_events flush failed: {exc}")
